"""Durable operation helpers: classification, adoption, baseline refresh.

Pure functions over the engine context and operation rows. Used by
:mod:`playlist_sync.engine.apply` (immediately after an uncertain write) and by
:mod:`playlist_sync.engine.reconcile` (on startup and before syncing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..backends import PlaylistNotFound
from ..matching.matcher import TrackMatcher
from ..models import RemotePlaylist, ServerKind, fingerprint_ids
from ..store import OperationState


@dataclass
class ReconcileOutcome:
    state: str
    detail: str
    read_back: RemotePlaylist | None = None
    adopted_playlist_id: str | None = None
    needs_intervention: bool = False


def safe_get_playlist(ctx, side: ServerKind, playlist_id: str | None) -> RemotePlaylist | None:
    if not playlist_id:
        return None
    try:
        return ctx.backend(side).get_playlist(playlist_id)
    except PlaylistNotFound:
        return None


def _desired_ids(op: dict) -> list[str]:
    try:
        return list(json.loads(op.get("desired_ids") or "[]"))
    except (TypeError, ValueError):
        return []


def find_created_playlist(
    ctx, side: ServerKind, desired_fp: str, intended_name: str | None
) -> tuple[str | None, bool]:
    """Locate a playlist that matches the intended contents after a lost create.

    Returns ``(playlist_id, ambiguous)``. Identification uses content fingerprint
    (and name for narrowing), never name alone. If more than one candidate
    matches, we return ``(None, True)`` and require the user to resolve it rather
    than risk creating a duplicate.
    """
    matches: list[str] = []
    for summary in ctx.backend(side).list_playlists(audio_only=False):
        playlist = safe_get_playlist(ctx, side, summary.playlist_id)
        if playlist is None:
            continue
        if playlist.fingerprint() != desired_fp:
            continue
        if intended_name and playlist.summary.name != intended_name:
            # name mismatch but content matches: still a candidate, but weaker
            pass
        matches.append(summary.playlist_id)
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0], False
    return None, len(matches) > 1


def classify_operation(ctx, op: dict) -> ReconcileOutcome:
    """Determine what a possibly-unfinished operation actually did."""
    direction = (op.get("direction") or "").lower()
    if direction == "backup":
        # Backups write files; there is no target playlist to verify, and a
        # repeat run is safe/idempotent. Never leave it "unfinished" forever.
        return ReconcileOutcome(
            state=OperationState.FAILED_SAFE,
            detail="the interrupted backup did not finish; backups are safe to run again",
        )
    desired_ids = _desired_ids(op)
    desired_fp = op.get("desired_fingerprint") or (fingerprint_ids(desired_ids) if desired_ids else None)
    before_fp = op.get("target_fingerprint_before")
    side = ServerKind(op.get("target_side") or "gonic")

    read_back = safe_get_playlist(ctx, side, op.get("target_playlist_id")) or safe_get_playlist(
        ctx, side, op.get("created_playlist_id")
    )

    if read_back is None and not op.get("target_playlist_id") and desired_ids and desired_fp:
        found_id, ambiguous = find_created_playlist(ctx, side, desired_fp, op.get("intended_name"))
        if ambiguous:
            return ReconcileOutcome(
                state=OperationState.RECOVERY_REQUIRED,
                detail=(
                    "a playlist appears to have been created but cannot be uniquely identified; "
                    "resolve the ambiguity before retrying"
                ),
                needs_intervention=True,
            )
        if found_id is not None:
            adopted = safe_get_playlist(ctx, side, found_id)
            return ReconcileOutcome(
                state=OperationState.SUCCEEDED,
                detail="adopted a playlist created by an earlier interrupted write",
                read_back=adopted,
                adopted_playlist_id=found_id,
            )
        return ReconcileOutcome(
            state=OperationState.FAILED_SAFE, detail="the playlist was not created"
        )

    if read_back is None:
        # target previously existed and is now missing
        return ReconcileOutcome(
            state=OperationState.RECOVERY_REQUIRED,
            detail="the target playlist is missing after an interrupted operation",
            needs_intervention=True,
        )

    fingerprint = read_back.fingerprint()
    if desired_fp and fingerprint == desired_fp:
        return ReconcileOutcome(
            state=OperationState.SUCCEEDED,
            detail="the target already matches the intended contents",
            read_back=read_back,
        )
    if before_fp and fingerprint == before_fp:
        return ReconcileOutcome(
            state=OperationState.FAILED_SAFE,
            detail="the target is unchanged from before the operation",
            read_back=read_back,
        )
    if not desired_ids and not read_back.entries:
        return ReconcileOutcome(
            state=OperationState.SUCCEEDED,
            detail="the target is empty as intended",
            read_back=read_back,
        )
    return ReconcileOutcome(
        state=OperationState.RECOVERY_REQUIRED,
        detail=(
            "the target changed but does not match the intended or previous contents; "
            "it may have been edited externally or partially applied"
        ),
        read_back=read_back,
        needs_intervention=True,
    )


def apply_outcome_to_operation(ctx, op: dict, outcome: ReconcileOutcome) -> None:
    updates: dict = {"state": outcome.state, "step": "reconciled", "detail": outcome.detail}
    if outcome.adopted_playlist_id:
        updates["created_playlist_id"] = outcome.adopted_playlist_id
        updates["target_playlist_id"] = outcome.adopted_playlist_id
    if outcome.read_back is not None:
        updates["target_fingerprint_after"] = outcome.read_back.fingerprint()
    ctx.store.update_operation(op["operation_key"], **updates)
    if outcome.needs_intervention:
        ctx.store.update_mapping(op["mapping_id"], paused=True)


def refresh_baseline(ctx, mapping) -> None:
    """Persist the current both-sides state as the new bidirectional baseline.

    Only call after a verified write (or verified no-change).
    """
    plex_pl = safe_get_playlist(ctx, ServerKind.PLEX, mapping.plex_playlist_id)
    gonic_pl = safe_get_playlist(ctx, ServerKind.GONIC, mapping.gonic_playlist_id)
    if plex_pl is None or gonic_pl is None:
        return
    gonic_index = ctx.library_index(ServerKind.GONIC)
    matcher = TrackMatcher(gonic_index, duration_tolerance_ms=ctx.settings.sync.duration_tolerance_ms)
    canonical: list[dict] = []
    for entry in plex_pl.entries:
        result = matcher.match(entry.track)
        canonical.append(
            {
                "plex_id": entry.track.track_id,
                "gonic_id": result.match.track_id if result.is_confident and result.match else None,
            }
        )
    ctx.store.set_baseline(
        mapping.id,
        plex_fingerprint=plex_pl.fingerprint(),
        gonic_fingerprint=gonic_pl.fingerprint(),
        plex_entries=[
            {"track_id": e.track.track_id, "occurrence_id": e.occurrence_id} for e in plex_pl.entries
        ],
        gonic_entries=[
            {"track_id": e.track.track_id, "occurrence_id": e.occurrence_id} for e in gonic_pl.entries
        ],
        canonical=canonical,
    )
    ctx.store.update_mapping(mapping.id, baseline_ready=True)
