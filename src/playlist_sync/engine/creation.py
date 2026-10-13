"""Standalone playlist creation with duplicate-name handling and dedupe.

Verified backends: Gonic supports creating an empty playlist; Plex does not
(creation requires items), so Plex empty creation is reported as unsupported and
the UI offers the copy/migrate wizard instead.

Duplicate names are never treated as identity: an existing same-name playlist is
never modified — a collision requires explicit confirmation to create another.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..errors import AppError
from ..matching.normalize import normalize_text
from ..models import PlaylistSummary, ServerKind
from ..store import OperationState
from .context import EngineContext
from .operations import apply_outcome_to_operation, classify_operation, safe_get_playlist

log = logging.getLogger("playlist_sync.engine.creation")


@dataclass
class CreationResult:
    status: str
    side: str = ""
    playlist_id: str | None = None
    name: str = ""
    detail: str = ""
    collisions: list[dict] = field(default_factory=list)
    operation_key: str | None = None
    created: bool = False


def duplicate_name_collisions(ctx: EngineContext, side: ServerKind, name: str) -> list[PlaylistSummary]:
    target = normalize_text(name)
    if not target:
        return []
    return [s for s in ctx.backend(side).list_playlists(audio_only=True) if normalize_text(s.name) == target]


def _dedupe(ctx: EngineContext, submission_key: str | None) -> CreationResult | None:
    if not submission_key:
        return None
    op = ctx.store.get_operation(submission_key)
    if op is None:
        return None
    if op["state"] == OperationState.SUCCEEDED:
        return CreationResult(
            status="already_created",
            side=op.get("target_side") or "",
            playlist_id=op.get("created_playlist_id") or op.get("target_playlist_id"),
            name=op.get("intended_name") or "",
            detail="This playlist was already created by an earlier submission.",
            operation_key=submission_key,
            created=True,
        )
    if op["state"] in {
        OperationState.PLANNED,
        OperationState.PREPARED,
        OperationState.APPLYING,
        OperationState.VERIFYING,
        OperationState.OUTCOME_UNKNOWN,
        OperationState.PARTIALLY_APPLIED,
        OperationState.RECOVERY_REQUIRED,
    }:
        return CreationResult(
            status="in_progress",
            side=op.get("target_side") or "",
            name=op.get("intended_name") or "",
            detail="A creation for this submission is already in progress or needs reconciliation.",
            operation_key=submission_key,
        )
    return None


def create_playlist(
    ctx: EngineContext,
    side: ServerKind,
    name: str,
    *,
    allow_duplicate: bool = False,
    submission_key: str | None = None,
) -> CreationResult:
    ctx.ensure_mutable()
    name = (name or "").strip()
    if not name:
        return CreationResult(status="error", side=side.value, detail="A playlist name is required.")

    existing = _dedupe(ctx, submission_key)
    if existing is not None:
        return existing

    backend = ctx.backend(side)
    if not backend.supports_empty_create():
        return CreationResult(
            status="unsupported",
            side=side.value,
            name=name,
            detail=(
                f"{side.value} cannot create an empty playlist through its API. "
                "Create it from an existing playlist instead (Copy to new playlist)."
            ),
        )

    collisions = duplicate_name_collisions(ctx, side, name)
    if collisions and not allow_duplicate:
        return CreationResult(
            status="needs_confirmation",
            side=side.value,
            name=name,
            detail=(
                f"{side.value} already has {len(collisions)} playlist(s) named {name!r}. "
                "Playlist names are not unique; confirm to create another (existing playlists are never modified)."
            ),
            collisions=[{"id": c.playlist_id, "name": c.name, "tracks": c.track_count} for c in collisions],
        )

    operation_key = ctx.store.create_operation(
        operation_key=submission_key,
        mapping_id=None,
        direction="create",
        source_side=None,
        target_side=side.value,
        intended_name=name,
        state=OperationState.PLANNED,
        step="planned",
        desired_ids=[],
    )
    ctx.store.update_operation(operation_key, state=OperationState.APPLYING, step="creating")
    try:
        playlist_id = backend.create_playlist(name, [])
    except Exception as exc:
        ctx.store.update_operation(
            operation_key,
            state=OperationState.OUTCOME_UNKNOWN,
            step="outcome_unknown",
            error_code=getattr(exc, "code", "INTERNAL"),
            detail=str(exc),
        )
        op = ctx.store.get_operation(operation_key)
        outcome = classify_operation(ctx, op) if op else None
        if outcome is not None:
            apply_outcome_to_operation(ctx, op, outcome)
        return CreationResult(
            status="outcome_unknown",
            side=side.value,
            name=name,
            detail=(
                "The result of the playlist creation is not yet known; it will be reconciled. "
                "Do not submit again."
            ),
            operation_key=operation_key,
        )

    ctx.store.update_operation(
        operation_key,
        state=OperationState.VERIFYING,
        step="verifying",
        created_playlist_id=playlist_id,
        target_playlist_id=playlist_id,
    )
    read_back = safe_get_playlist(ctx, side, playlist_id)
    if read_back is None:
        ctx.store.update_operation(
            operation_key, state=OperationState.RECOVERY_REQUIRED, step="verify", detail="not readable"
        )
        return CreationResult(
            status="created_unverified",
            side=side.value,
            name=name,
            playlist_id=playlist_id,
            detail="The playlist was created but could not be read back for verification.",
            operation_key=operation_key,
            created=True,
        )
    ctx.store.update_operation(
        operation_key,
        state=OperationState.SUCCEEDED,
        step="verified",
        target_fingerprint_after=read_back.fingerprint(),
    )
    _refresh_discovery(ctx, side)
    return CreationResult(
        status="created",
        side=side.value,
        name=read_back.summary.name,
        playlist_id=playlist_id,
        detail="Playlist created and verified.",
        operation_key=operation_key,
        created=True,
    )


def _refresh_discovery(ctx: EngineContext, side: ServerKind) -> None:
    try:
        from .discover import discovery_playlists

        discovery_playlists(ctx, side, cache=True)
    except AppError:
        pass
    except Exception as exc:
        log.warning("could not refresh %s discovery cache: %s", side.value, exc)


__all__ = ["CreationResult", "create_playlist", "duplicate_name_collisions"]
