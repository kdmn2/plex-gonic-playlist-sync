"""Copy an existing playlist into a newly created destination playlist.

Primary flows: Plex -> new Gonic, Gonic -> new Plex. This is a **copy**: the
source playlist and audio files are never modified or deleted. It reuses the
existing matcher, durable operation journal and baseline machinery.

Confirmation is bound to the reviewed plan (source snapshot fingerprint,
destination settings, matching decisions and desired order). Ongoing
synchronization is enabled only after the initial population is verified and its
baseline is persisted.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from ..matching.matcher import TrackMatcher
from ..models import PlannedEntry, RemotePlaylist, ServerKind, fingerprint_ids
from ..store import OperationState
from .confirm import issue_token, verify_token
from .context import EngineContext
from .creation import _refresh_discovery, duplicate_name_collisions
from .operations import refresh_baseline, safe_get_playlist
from .verify import verify_sequence

log = logging.getLogger("playlist_sync.engine.migration")


@dataclass
class MigrationPlan:
    source_side: ServerKind
    target_side: ServerKind
    name: str
    source_playlist: RemotePlaylist | None = None
    desired_ids: list[str] = field(default_factory=list)
    matched: list[PlannedEntry] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    collisions: list[dict] = field(default_factory=list)
    blocked: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fingerprint: str = ""
    allow_partial: bool = False
    enable_ongoing: bool = False

    @property
    def direction(self) -> str:
        return (
            "plex_to_gonic" if self.source_side == ServerKind.PLEX else "gonic_to_plex"
        )

    def summary(self) -> dict:
        return {
            "source": self.source_side.value,
            "target": self.target_side.value,
            "name": self.name,
            "tracks_in_source": len(self.source_playlist.entries) if self.source_playlist else 0,
            "matched": len(self.desired_ids),
            "unmatched": len(self.unmatched),
            "collisions": len(self.collisions),
            "blocked": self.blocked,
            "reasons": list(self.blocked_reasons),
            "fingerprint": self.fingerprint,
        }


def migration_fingerprint(
    source_side: str, target_side: str, name: str, source_fp: str, desired_ids: list[str]
) -> str:
    parts = [source_side, target_side, name, source_fp, "\x00".join(desired_ids)]
    return hashlib.sha256("\x01".join(parts).encode("utf-8")).hexdigest()


def plan_migration(
    ctx: EngineContext,
    source_side: ServerKind,
    source_playlist_id: str,
    target_side: ServerKind,
    name: str,
    *,
    allow_partial: bool = False,
    enable_ongoing: bool = False,
    progress=None,
) -> MigrationPlan:
    plan = MigrationPlan(
        source_side=source_side,
        target_side=target_side,
        name=(name or "").strip(),
        allow_partial=allow_partial,
        enable_ongoing=enable_ongoing,
    )
    if not plan.name:
        plan.blocked = True
        plan.blocked_reasons.append("A destination playlist name is required.")
        return plan

    source = safe_get_playlist(ctx, source_side, source_playlist_id)
    if source is None:
        plan.blocked = True
        plan.blocked_reasons.append("The source playlist no longer exists.")
        return plan
    plan.source_playlist = source

    target_index = ctx.ensure_library_index(target_side, progress=progress)
    matcher = TrackMatcher(target_index, duration_tolerance_ms=ctx.settings.sync.duration_tolerance_ms)

    for entry in source.entries:
        result = matcher.match(entry.track)
        if result.is_confident and result.match is not None:
            plan.desired_ids.append(result.match.track_id)
            plan.matched.append(
                PlannedEntry(entry.index, entry.track, result.match, result)
            )
        else:
            plan.unmatched.append(
                {
                    "index": entry.index,
                    "track_id": entry.track.track_id,
                    "label": entry.track.identity_hint(),
                    "status": result.status,
                    "detail": result.detail,
                }
            )

    block_unmatched = ctx.settings.sync.block_on_unmatched and not allow_partial
    if plan.unmatched and block_unmatched:
        plan.blocked = True
        plan.blocked_reasons.append(
            f"{len(plan.unmatched)} source track(s) could not be matched confidently; the copy is blocked "
            "by default (opt into partial copy to skip them)."
        )

    if not plan.desired_ids and not ctx.backend(target_side).supports_empty_create():
        plan.blocked = True
        plan.blocked_reasons.append(
            f"{target_side.value} cannot create an empty playlist; no tracks were matched, so a copy "
            "cannot be created."
        )

    if source.summary.is_smart:
        plan.notes.append(
            "The Plex source is a smart playlist. A one-time copy captures its current evaluated contents; "
            "ongoing sync follows later evaluations. It is never written to."
        )

    plan.collisions = [
        {"id": c.playlist_id, "name": c.name, "tracks": c.track_count}
        for c in duplicate_name_collisions(ctx, target_side, plan.name)
    ]
    plan.fingerprint = migration_fingerprint(
        source_side.value,
        target_side.value,
        plan.name,
        source.fingerprint(),
        plan.desired_ids,
    )
    return plan


def confirmation_reasons(plan: MigrationPlan) -> list[str]:
    reasons: list[str] = []
    if plan.collisions:
        reasons.append(
            f"{plan.target_side.value} already has {len(plan.collisions)} playlist(s) named {plan.name!r}"
        )
    if plan.unmatched and plan.allow_partial:
        reasons.append(f"partial copy will skip {len(plan.unmatched)} unmatched track(s)")
    return reasons


@dataclass
class MigrationResult:
    status: str
    detail: str = ""
    playlist_id: str | None = None
    name: str = ""
    mapping_id: str | None = None
    operation_key: str | None = None
    unmatched: list[dict] = field(default_factory=list)
    collisions: list[dict] = field(default_factory=list)
    needs_confirmation: bool = False
    confirmation_token: str | None = None
    verification: str = ""


def _dedupe(ctx: EngineContext, fingerprint: str) -> MigrationResult | None:
    op = ctx.store.get_operation(fingerprint)
    if op is None:
        return None
    if op["state"] == OperationState.SUCCEEDED:
        mapping_id = op.get("mapping_id")
        return MigrationResult(
            status="already_copied",
            detail="This copy was already completed.",
            playlist_id=op.get("created_playlist_id") or op.get("target_playlist_id"),
            name=op.get("intended_name") or "",
            mapping_id=mapping_id,
            operation_key=fingerprint,
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
        return MigrationResult(
            status="in_progress",
            detail="A copy for this plan is already in progress or needs reconciliation.",
            operation_key=fingerprint,
        )
    return None


def apply_migration(
    ctx: EngineContext,
    plan: MigrationPlan,
    *,
    enable_ongoing: bool,
    confirm_token: str | None = None,
    force: bool = False,
    secret: str | None = None,
    progress=None,
) -> MigrationResult:
    ctx.ensure_mutable()
    if plan.blocked:
        return MigrationResult(
            status="blocked",
            detail="; ".join(plan.blocked_reasons),
            unmatched=plan.unmatched,
            collisions=plan.collisions,
        )

    reasons = confirmation_reasons(plan)
    if reasons and not force and not verify_token(secret, f"migrate:{plan.fingerprint}", plan.fingerprint, confirm_token):
        return MigrationResult(
            status="needs_confirmation",
            detail="This copy needs confirmation: " + "; ".join(reasons),
            needs_confirmation=True,
            confirmation_token=issue_token(secret, f"migrate:{plan.fingerprint}", plan.fingerprint),
            unmatched=plan.unmatched,
            collisions=plan.collisions,
        )

    existing = _dedupe(ctx, plan.fingerprint)
    if existing is not None:
        return existing

    # Revalidate the source against the exact reviewed snapshot.
    fresh_source = safe_get_playlist(ctx, plan.source_side, plan.source_playlist.playlist_id)
    if fresh_source is None or fresh_source.fingerprint() != plan.source_playlist.fingerprint():
        return MigrationResult(
            status="stale",
            detail="The source playlist changed since the preview; review a fresh preview before copying.",
        )

    operation_key = ctx.store.create_operation(
        operation_key=plan.fingerprint,
        mapping_id=None,
        direction=plan.direction,
        source_side=plan.source_side.value,
        target_side=plan.target_side.value,
        source_playlist_id=plan.source_playlist.playlist_id,
        intended_name=plan.name,
        state=OperationState.PLANNED,
        step="migration_planned",
        source_fingerprint=plan.source_playlist.fingerprint(),
        desired_fingerprint=fingerprint_ids(plan.desired_ids),
        desired_ids=plan.desired_ids,
    )
    ctx.store.update_operation(operation_key, state=OperationState.APPLYING, step="creating_destination")
    if progress is not None:
        progress.raise_if_cancelled("Cancellation requested before writing; nothing was changed.")
        progress.stage(
            "creating", "Creating destination playlist",
            completed=0, total=len(plan.desired_ids), unit="tracks",
            counters={"planned": len(plan.desired_ids)},
        )
    backend = ctx.backend(plan.target_side)
    try:
        playlist_id = backend.create_playlist(plan.name, plan.desired_ids)
    except Exception as exc:
        ctx.store.update_operation(
            operation_key,
            state=OperationState.OUTCOME_UNKNOWN,
            step="outcome_unknown",
            error_code=getattr(exc, "code", "INTERNAL"),
            detail=str(exc),
        )
        from .operations import apply_outcome_to_operation, classify_operation

        op = ctx.store.get_operation(operation_key)
        outcome = classify_operation(ctx, op) if op else None
        if outcome is not None:
            apply_outcome_to_operation(ctx, op, outcome)
        return MigrationResult(
            status="outcome_unknown",
            detail="The copy outcome is not yet known; it will be reconciled. Do not submit again.",
            operation_key=operation_key,
        )

    ctx.store.update_operation(
        operation_key,
        state=OperationState.VERIFYING,
        step="verifying",
        created_playlist_id=playlist_id,
        target_playlist_id=playlist_id,
    )
    if progress is not None:
        progress.stage(
            "verifying", "Verifying destination",
            completed=0, total=len(plan.desired_ids), unit="entries",
            counters={"confirmed": len(plan.desired_ids)},
        )
    read_back = safe_get_playlist(ctx, plan.target_side, playlist_id)
    ok, detail = (False, "destination not readable")
    if read_back is not None:
        ok, detail = verify_sequence(read_back, plan.desired_ids)
    if not ok:
        ctx.store.update_operation(
            operation_key,
            state=OperationState.RECOVERY_REQUIRED,
            step="verify",
            detail=detail,
        )
        return MigrationResult(
            status="created_population_incomplete",
            detail=(
                f"A new playlist was created but verification failed: {detail}. "
                "It will not be used for ongoing sync until reconciled."
            ),
            playlist_id=playlist_id,
            name=plan.name,
            operation_key=operation_key,
            verification=detail,
        )

    ctx.store.save_snapshot(
        mapping_id=None,
        side=plan.target_side.value,
        playlist_id=playlist_id,
        kind="last_success",
        entries=[{"track_id": e.track.track_id, "occurrence_id": e.occurrence_id} for e in read_back.entries],
        fingerprint=read_back.fingerprint(),
    )
    _refresh_discovery(ctx, plan.target_side)

    mapping_id = None
    mapping_note = ""
    if enable_ongoing:
        try:
            mapping = ctx.store.create_mapping(
                name=f"{plan.name} ({plan.source_side.value} → {plan.target_side.value})",
                direction=plan.direction,
                enabled=True,
                plex_playlist_id=plan.source_playlist.playlist_id
                if plan.source_side == ServerKind.PLEX
                else playlist_id,
                gonic_playlist_id=plan.source_playlist.playlist_id
                if plan.source_side == ServerKind.GONIC
                else playlist_id,
            )
            mapping_id = mapping.id
            ctx.store.update_operation(operation_key, mapping_id=mapping_id)
            try:
                refresh_baseline(ctx, mapping)
            except Exception as exc:
                ctx.store.update_mapping(mapping.id, paused=True)
                mapping_note = f" (ongoing sync created but paused for revalidation: {type(exc).__name__})"
        except Exception as exc:
            mapping_note = (
                " Copy succeeded but ongoing sync was not enabled. "
                f"Attach a mapping to the new playlist manually ({type(exc).__name__})."
            )

    ctx.store.update_operation(
        operation_key,
        state=OperationState.SUCCEEDED,
        step="verified",
        target_fingerprint_after=read_back.fingerprint(),
    )
    if enable_ongoing:
        if mapping_id and "paused" in mapping_note:
            status = "copied_mapping_paused"
        elif mapping_id:
            status = "copied_mapping_enabled"
        else:
            status = "copy_succeeded_mapping_failed"
    else:
        status = "copied"
    return MigrationResult(
        status=status,
        detail=f"Copy verified ({detail}).{mapping_note}",
        playlist_id=playlist_id,
        name=read_back.summary.name,
        mapping_id=mapping_id,
        operation_key=operation_key,
        verification=detail,
    )


__all__ = [
    "MigrationPlan",
    "MigrationResult",
    "apply_migration",
    "confirmation_reasons",
    "migration_fingerprint",
    "plan_migration",
]
