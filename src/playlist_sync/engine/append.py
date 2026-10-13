"""Append missing tracks to an existing playlist, without duplicates.

Adds tracks that are not already present in the destination. Existing entries,
their order, and their duplicate occurrences remain unchanged.

Algorithm (membership by *resolved destination track identity*, never by
playlist-entry id, server-local id, or title alone):
1. read the complete ordered destination;
2. build a membership set of its destination track ids;
3. traverse the source in order, resolving each entry to a destination track;
4. skip tracks already in the destination;
5. skip tracks already selected earlier in this operation;
6. append the rest in source order, at most once each.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from ..matching.matcher import TrackMatcher
from ..models import RemotePlaylist, ServerKind, fingerprint_ids
from ..store import OperationState
from .confirm import issue_token, verify_token
from .context import EngineContext
from .creation import _refresh_discovery
from .operations import safe_get_playlist
from .verify import verify_sequence

log = logging.getLogger("playlist_sync.engine.append")


@dataclass
class AppendPlan:
    source_side: ServerKind
    dest_side: ServerKind
    source_id: str
    dest_id: str
    source_playlist: RemotePlaylist | None = None
    dest_playlist: RemotePlaylist | None = None
    additions: list[str] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    existing_count: int = 0
    source_count: int = 0
    skipped_present: int = 0
    skipped_repeat: int = 0
    final_count: int = 0
    blocked: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fingerprint: str = ""
    dest_fingerprint: str = ""
    allow_partial: bool = False

    def summary(self) -> dict:
        return {
            "source": self.source_side.value,
            "destination": self.dest_side.value,
            "existing_entries": self.existing_count,
            "source_entries": self.source_count,
            "skipped_present": self.skipped_present,
            "skipped_repeat": self.skipped_repeat,
            "unmatched": len(self.unmatched),
            "additions": len(self.additions),
            "final_entries": self.final_count,
            "blocked": self.blocked,
            "reasons": list(self.blocked_reasons),
            "fingerprint": self.fingerprint,
        }


def _resolve(ctx: EngineContext, source_side: ServerKind, dest_side: ServerKind, track, matcher):
    if source_side == dest_side:
        return track.track_id
    result = matcher.match(track)
    if result.is_confident and result.match is not None:
        return result.match.track_id
    return result


def plan_append(
    ctx: EngineContext,
    source_side: ServerKind,
    source_id: str,
    dest_side: ServerKind,
    dest_id: str,
    *,
    allow_partial: bool = False,
) -> AppendPlan:
    plan = AppendPlan(
        source_side=source_side, dest_side=dest_side, source_id=source_id, dest_id=dest_id,
        allow_partial=allow_partial,
    )
    if source_side == dest_side and source_id == dest_id:
        plan.blocked = True
        plan.blocked_reasons.append("Source and destination are the same playlist.")
        return plan

    plan.source_playlist = safe_get_playlist(ctx, source_side, source_id)
    plan.dest_playlist = safe_get_playlist(ctx, dest_side, dest_id)
    if plan.source_playlist is None:
        plan.blocked = True
        plan.blocked_reasons.append("The source playlist no longer exists.")
        return plan
    if plan.dest_playlist is None:
        plan.blocked = True
        plan.blocked_reasons.append("The destination playlist no longer exists.")
        return plan
    if dest_side == ServerKind.PLEX and plan.dest_playlist.summary.is_smart:
        plan.blocked = True
        plan.blocked_reasons.append("A Plex smart playlist cannot be a destination (read-only).")
        return plan
    if plan.dest_playlist.summary.writable is False:
        plan.blocked = True
        plan.blocked_reasons.append("The configured account cannot write to the destination playlist.")
        return plan

    plan.existing_count = len(plan.dest_playlist.entries)
    plan.source_count = len(plan.source_playlist.entries)
    membership = set(plan.dest_playlist.track_ids())
    added: set[str] = set()
    matcher = None
    if source_side != dest_side:
        index = ctx.library_index(dest_side)
        matcher = TrackMatcher(index, duration_tolerance_ms=ctx.settings.sync.duration_tolerance_ms)

    for entry in plan.source_playlist.entries:
        resolved = _resolve(ctx, source_side, dest_side, entry.track, matcher)
        if isinstance(resolved, str):
            tid = resolved
        else:
            plan.unmatched.append(
                {
                    "track_id": entry.track.track_id,
                    "label": entry.track.identity_hint(),
                    "status": resolved.status,
                    "detail": resolved.detail,
                }
            )
            continue
        if tid in membership:
            plan.skipped_present += 1
            continue
        if tid in added:
            plan.skipped_repeat += 1
            continue
        plan.additions.append(tid)
        added.add(tid)

    if plan.unmatched and ctx.settings.sync.block_on_unmatched and not allow_partial:
        plan.blocked = True
        plan.blocked_reasons.append(
            f"{len(plan.unmatched)} source track(s) could not be matched confidently; blocked by "
            "default (opt into partial append to skip them)."
        )

    plan.final_count = plan.existing_count + len(plan.additions)
    plan.dest_fingerprint = plan.dest_playlist.fingerprint()
    parts = [
        plan.dest_side.value, plan.dest_id, plan.dest_fingerprint,
        plan.source_side.value, plan.source_id, plan.source_playlist.fingerprint(),
        "\x00".join(plan.additions),
    ]
    plan.fingerprint = hashlib.sha256("\x01".join(parts).encode("utf-8")).hexdigest()
    return plan


def confirmation_reasons(plan: AppendPlan) -> list[str]:
    if plan.unmatched and plan.allow_partial:
        return [f"partial append will skip {len(plan.unmatched)} unmatched track(s)"]
    return []


@dataclass
class AppendResult:
    status: str
    detail: str = ""
    dest_id: str | None = None
    name: str = ""
    additions: int = 0
    skipped_present: int = 0
    skipped_repeat: int = 0
    unmatched: list[dict] = field(default_factory=list)
    final_count: int = 0
    operation_key: str | None = None
    needs_confirmation: bool = False
    confirmation_token: str | None = None
    verification: str = ""


def _dedupe(ctx, fingerprint: str) -> AppendResult | None:
    op = ctx.store.get_operation(fingerprint)
    if op is None:
        return None
    if op["state"] == OperationState.SUCCEEDED:
        return AppendResult(
            status="already_appended",
            detail="These additions were already applied.",
            dest_id=op.get("target_playlist_id"),
            operation_key=fingerprint,
        )
    if op["state"] in {
        OperationState.PLANNED, OperationState.PREPARED, OperationState.APPLYING,
        OperationState.VERIFYING, OperationState.OUTCOME_UNKNOWN,
        OperationState.PARTIALLY_APPLIED, OperationState.RECOVERY_REQUIRED,
    }:
        return AppendResult(
            status="in_progress",
            detail="An append for this plan is already in progress or needs reconciliation.",
            operation_key=fingerprint,
        )
    return None


def apply_append(
    ctx: EngineContext,
    plan: AppendPlan,
    *,
    confirm_token: str | None = None,
    force: bool = False,
    secret: str | None = None,
) -> AppendResult:
    ctx.ensure_mutable()
    if plan.blocked:
        return AppendResult(status="blocked", detail="; ".join(plan.blocked_reasons), unmatched=plan.unmatched)
    if not plan.additions:
        return AppendResult(
            status="no_change",
            detail="Already up to date.",
            dest_id=plan.dest_id,
            name=plan.dest_playlist.summary.name if plan.dest_playlist else "",
            skipped_present=plan.skipped_present,
            skipped_repeat=plan.skipped_repeat,
            final_count=plan.final_count,
        )
    reasons = confirmation_reasons(plan)
    if reasons and not force and not verify_token(secret, f"append:{plan.fingerprint}", plan.fingerprint, confirm_token):
        return AppendResult(
            status="needs_confirmation",
            detail="This append needs confirmation: " + "; ".join(reasons),
            needs_confirmation=True,
            confirmation_token=issue_token(secret, f"append:{plan.fingerprint}", plan.fingerprint),
            unmatched=plan.unmatched,
        )
    existing = _dedupe(ctx, plan.fingerprint)
    if existing is not None:
        return existing

    fresh = safe_get_playlist(ctx, plan.dest_side, plan.dest_id)
    if fresh is None or fresh.fingerprint() != plan.dest_fingerprint:
        return AppendResult(
            status="stale",
            detail="The destination playlist changed since the preview; review a fresh preview.",
            dest_id=plan.dest_id,
        )

    expected = list(fresh.track_ids()) + list(plan.additions)
    operation_key = ctx.store.create_operation(
        operation_key=plan.fingerprint,
        direction="append",
        source_side=plan.source_side.value,
        target_side=plan.dest_side.value,
        source_playlist_id=plan.source_id,
        target_playlist_id=plan.dest_id,
        intended_name=fresh.summary.name,
        state=OperationState.PLANNED,
        step="append_planned",
        source_fingerprint=plan.source_playlist.fingerprint() if plan.source_playlist else None,
        target_fingerprint_before=plan.dest_fingerprint,
        desired_fingerprint=fingerprint_ids(plan.additions),
        desired_ids=plan.additions,
    )
    snapshot_id = ctx.store.save_snapshot(
        mapping_id=None,
        side=plan.dest_side.value,
        playlist_id=plan.dest_id,
        kind="pre_write",
        entries=[{"track_id": e.track.track_id, "occurrence_id": e.occurrence_id} for e in fresh.entries],
        fingerprint=fresh.fingerprint(),
    )
    ctx.store.update_operation(operation_key, state=OperationState.APPLYING, step="appending", snapshot_id=snapshot_id)
    backend = ctx.backend(plan.dest_side)
    try:
        backend.append_entries(plan.dest_id, plan.additions)
    except Exception as exc:
        from .operations import apply_outcome_to_operation, classify_operation

        ctx.store.update_operation(
            operation_key, state=OperationState.OUTCOME_UNKNOWN, step="outcome_unknown",
            error_code=getattr(exc, "code", "INTERNAL"), detail=str(exc),
        )
        op = ctx.store.get_operation(operation_key)
        outcome = classify_operation(ctx, op) if op else None
        if outcome is not None:
            apply_outcome_to_operation(ctx, op, outcome)
        return AppendResult(
            status="outcome_unknown",
            detail="The append outcome is not yet known; it will be reconciled. Do not submit again.",
            dest_id=plan.dest_id,
            operation_key=operation_key,
        )

    ctx.store.update_operation(operation_key, state=OperationState.VERIFYING, step="verifying")
    read_back = safe_get_playlist(ctx, plan.dest_side, plan.dest_id)
    ok, detail = (False, "destination not readable")
    if read_back is not None:
        ok, detail = verify_sequence(read_back, expected)
    if not ok:
        ctx.store.update_operation(
            operation_key, state=OperationState.RECOVERY_REQUIRED, step="verify", detail=detail
        )
        return AppendResult(
            status="recovery_required",
            detail=f"The destination did not verify after append: {detail}",
            dest_id=plan.dest_id,
            operation_key=operation_key,
            verification=detail,
        )

    ctx.store.save_snapshot(
        mapping_id=None, side=plan.dest_side.value, playlist_id=plan.dest_id, kind="last_success",
        entries=[{"track_id": e.track.track_id, "occurrence_id": e.occurrence_id} for e in read_back.entries],
        fingerprint=read_back.fingerprint(),
    )
    _refresh_discovery(ctx, plan.dest_side)
    ctx.store.update_operation(
        operation_key, state=OperationState.SUCCEEDED, step="verified",
        target_fingerprint_after=read_back.fingerprint(),
    )
    return AppendResult(
        status="appended",
        detail=f"Appended {len(plan.additions)} track(s); existing entries unchanged ({detail}).",
        dest_id=plan.dest_id,
        name=read_back.summary.name,
        additions=len(plan.additions),
        skipped_present=plan.skipped_present,
        skipped_repeat=plan.skipped_repeat,
        final_count=len(expected),
        operation_key=operation_key,
        verification=detail,
    )


__all__ = ["AppendPlan", "AppendResult", "apply_append", "confirmation_reasons", "plan_append"]
