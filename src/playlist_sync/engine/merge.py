"""Merge any two accessible playlists into a new static playlist.

Supports Plex+Plex, Gonic+Gonic, Plex+Gonic and Gonic+Plex. This is a one-time
operation: it never creates a scheduled mapping, never modifies the sources, and
never touches audio files. It reuses the matcher, the durable operation journal,
verification, and the dedupe/confirmation machinery.

Duplicate policies (applied only after resolving tracks to the destination):
- keep_all: keep every occurrence from both inputs in the chosen order.
- remove_overlaps: keep the first playlist exactly (incl. its duplicates); from
  the second, omit any occurrence whose resolved destination track already
  appears anywhere in the first.
- first_occurrence: keep the first occurrence of each resolved destination track.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from ..matching.matcher import TrackMatcher
from ..models import RemotePlaylist, RemoteTrack, ServerKind, fingerprint_ids
from ..store import OperationState
from .confirm import issue_token, verify_token
from .context import EngineContext
from .creation import _refresh_discovery, duplicate_name_collisions
from .operations import safe_get_playlist
from .verify import verify_sequence

log = logging.getLogger("playlist_sync.engine.merge")

POLICIES = ("keep_all", "remove_overlaps", "first_occurrence")
ORDERS = ("ab", "ba")


@dataclass
class MergePlan:
    target_side: ServerKind
    name: str
    order: str = "ab"
    policy: str = "keep_all"
    source_a: tuple[ServerKind, str] | None = None
    source_b: tuple[ServerKind, str] | None = None
    playlist_a: RemotePlaylist | None = None
    playlist_b: RemotePlaylist | None = None
    desired_ids: list[str] = field(default_factory=list)
    # Resolved destination tracks for the ordered result, keyed by track id, so
    # the UI can render readable labels without re-resolving metadata.
    target_tracks: dict[str, RemoteTrack] = field(default_factory=dict)
    unmatched: list[dict] = field(default_factory=list)
    collisions: list[dict] = field(default_factory=list)
    policy_effects: dict[str, int] = field(default_factory=dict)
    removed: int = 0
    blocked: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fingerprint: str = ""
    allow_partial: bool = False

    def summary(self) -> dict:
        return {
            "order": self.order,
            "policy": self.policy,
            "target": self.target_side.value,
            "name": self.name,
            "a_entries": len(self.playlist_a.entries) if self.playlist_a else 0,
            "b_entries": len(self.playlist_b.entries) if self.playlist_b else 0,
            "final_entries": len(self.desired_ids),
            "removed": self.removed,
            "unmatched": len(self.unmatched),
            "collisions": len(self.collisions),
            "blocked": self.blocked,
            "reasons": list(self.blocked_reasons),
            "policy_effects": self.policy_effects,
            "fingerprint": self.fingerprint,
        }


def _source_entries(plan: MergePlan):
    """Ordered (side, entry) pairs for A then B or B then A."""
    a = [(plan.source_a[0], e) for e in (plan.playlist_a.entries if plan.playlist_a else [])]
    b = [(plan.source_b[0], e) for e in (plan.playlist_b.entries if plan.playlist_b else [])]
    return a + b if plan.order == "ab" else b + a


def _resolve_ids(ctx: EngineContext, plan: MergePlan, matcher_by_side, progress=None) -> list[tuple[str, str | None]]:
    """Resolve the ordered entries to destination track ids (None = unmatched)."""
    resolved: list[tuple[str, str | None]] = []
    entries = _source_entries(plan)
    total = len(entries)
    if progress is not None:
        progress.stage("matching", "Matching tracks", completed=0, total=total, unit="entries")
    for index, (side, entry) in enumerate(entries):
        if side == plan.target_side:
            # Same server: the source id is already a destination id.
            tid = entry.track.track_id
            resolved.append((side, tid))
            plan.target_tracks.setdefault(tid, entry.track)
        else:
            matcher = matcher_by_side.get(side)
            result = matcher.match(entry.track)
            if result.is_confident and result.match is not None:
                tid = result.match.track_id
                resolved.append((side, tid))
                plan.target_tracks.setdefault(tid, result.match)
            else:
                resolved.append((side, None))
                plan.unmatched.append(
                    {
                        "side": side.value,
                        "track_id": entry.track.track_id,
                        "label": entry.track.identity_hint(),
                        "status": result.status,
                        "detail": result.detail,
                    }
                )
        if progress is not None and (index % 25 == 0 or index == total - 1):
            matched = sum(1 for _s, t in resolved if t is not None)
            progress.progress(
                completed=index + 1,
                counters={
                    "processed": index + 1,
                    "matched": matched,
                    "unmatched": len(plan.unmatched),
                },
                desc=f"Matching tracks — {index + 1} of {total} entries processed",
                force=(index == total - 1),
            )
    return resolved


def _apply_policy(ids: list[str], first_count: int, policy: str) -> list[str]:
    if policy == "keep_all":
        return list(ids)
    if policy == "remove_overlaps":
        first = ids[:first_count]
        first_set = set(first)
        second = [tid for tid in ids[first_count:] if tid not in first_set]
        return first + second
    if policy == "first_occurrence":
        seen: set[str] = set()
        out: list[str] = []
        for tid in ids:
            if tid in seen:
                continue
            seen.add(tid)
            out.append(tid)
        return out
    return list(ids)


def merge_fingerprint(plan: MergePlan) -> str:
    parts = [
        plan.order,
        plan.policy,
        plan.target_side.value,
        plan.name,
    ]
    for side, playlist in ((plan.source_a, plan.playlist_a), (plan.source_b, plan.playlist_b)):
        parts.append(side[0].value if side else "")
        parts.append(side[1] if side else "")
        parts.append(playlist.fingerprint() if playlist else "")
    parts.append("\x00".join(plan.desired_ids))
    return hashlib.sha256("\x01".join(parts).encode("utf-8")).hexdigest()


def plan_merge(
    ctx: EngineContext,
    source_a: tuple[ServerKind, str],
    source_b: tuple[ServerKind, str],
    target_side: ServerKind,
    name: str,
    *,
    order: str = "ab",
    policy: str = "keep_all",
    allow_partial: bool = False,
    progress=None,
) -> MergePlan:
    plan = MergePlan(target_side=target_side, name=(name or "").strip(), order=order, policy=policy)
    plan.source_a, plan.source_b = source_a, source_b
    plan.allow_partial = allow_partial
    if not plan.name:
        plan.blocked = True
        plan.blocked_reasons.append("A destination playlist name is required.")
        return plan
    if order not in ORDERS:
        order = "ab"
        plan.order = order
    if policy not in POLICIES:
        policy = "keep_all"
        plan.policy = policy

    plan.playlist_a = safe_get_playlist(ctx, source_a[0], source_a[1])
    plan.playlist_b = safe_get_playlist(ctx, source_b[0], source_b[1])
    if plan.playlist_a is None or plan.playlist_b is None:
        plan.blocked = True
        plan.blocked_reasons.append("One or both source playlists no longer exist.")
        return plan

    target_index = ctx.ensure_library_index(target_side, progress=progress)
    matcher_by_side = {
        ServerKind.PLEX: TrackMatcher(target_index, duration_tolerance_ms=ctx.settings.sync.duration_tolerance_ms),
        ServerKind.GONIC: TrackMatcher(target_index, duration_tolerance_ms=ctx.settings.sync.duration_tolerance_ms),
    }
    resolved = _resolve_ids(ctx, plan, matcher_by_side, progress)

    block_unmatched = ctx.settings.sync.block_on_unmatched and not allow_partial
    if plan.unmatched and block_unmatched:
        plan.blocked = True
        plan.blocked_reasons.append(
            f"{len(plan.unmatched)} source track(s) could not be matched confidently; the merge is "
            "blocked by default (opt into partial merge to skip them)."
        )

    sequence = [tid for _side, tid in resolved if tid is not None]
    entries = _source_entries(plan)
    first_block = len(plan.playlist_a.entries) if order == "ab" else len(plan.playlist_b.entries)
    first_ids = [tid for (_side, tid) in resolved[:first_block] if tid is not None]
    first_count = len(first_ids)
    _ = entries  # kept for clarity; first_block derives from the chosen order

    plan.policy_effects = {
        p: len(sequence) - len(_apply_policy(sequence, first_count, p)) for p in POLICIES
    }
    final = _apply_policy(sequence, first_count, policy)
    plan.desired_ids = final
    plan.removed = len(sequence) - len(final)

    if not final and not ctx.backend(target_side).supports_empty_create():
        plan.blocked = True
        plan.blocked_reasons.append(
            f"{target_side.value} cannot create an empty playlist; the merged result is empty."
        )

    if (plan.playlist_a and plan.playlist_a.summary.is_smart) or (
        plan.playlist_b and plan.playlist_b.summary.is_smart
    ):
        plan.notes.append(
            "A smart Plex source is used as a read-only snapshot of its current evaluated contents."
        )
    plan.collisions = [
        {"id": c.playlist_id, "name": c.name, "tracks": c.track_count}
        for c in duplicate_name_collisions(ctx, target_side, plan.name)
    ]
    plan.fingerprint = merge_fingerprint(plan)
    return plan


def confirmation_reasons(plan: MergePlan) -> list[str]:
    reasons: list[str] = []
    if plan.collisions:
        reasons.append(
            f"{plan.target_side.value} already has {len(plan.collisions)} playlist(s) named {plan.name!r}"
        )
    if plan.unmatched and plan.allow_partial:
        reasons.append(f"partial merge will skip {len(plan.unmatched)} unmatched track(s)")
    return reasons


@dataclass
class MergeResult:
    status: str
    detail: str = ""
    playlist_id: str | None = None
    name: str = ""
    operation_key: str | None = None
    removed: int = 0
    final_entries: int = 0
    unmatched: list[dict] = field(default_factory=list)
    collisions: list[dict] = field(default_factory=list)
    needs_confirmation: bool = False
    confirmation_token: str | None = None
    verification: str = ""


def _dedupe(ctx, fingerprint: str) -> MergeResult | None:
    op = ctx.store.get_operation(fingerprint)
    if op is None:
        return None
    if op["state"] == OperationState.SUCCEEDED:
        return MergeResult(
            status="already_merged",
            detail="This merge was already completed.",
            playlist_id=op.get("created_playlist_id") or op.get("target_playlist_id"),
            name=op.get("intended_name") or "",
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
        return MergeResult(
            status="in_progress",
            detail="A merge for this plan is already in progress or needs reconciliation.",
            operation_key=fingerprint,
        )
    return None


def apply_merge(
    ctx: EngineContext,
    plan: MergePlan,
    *,
    confirm_token: str | None = None,
    force: bool = False,
    secret: str | None = None,
    progress=None,
    operation_key: str | None = None,
) -> MergeResult:
    ctx.ensure_mutable()
    if plan.blocked:
        return MergeResult(
            status="blocked",
            detail="; ".join(plan.blocked_reasons),
            unmatched=plan.unmatched,
            collisions=plan.collisions,
        )
    reasons = confirmation_reasons(plan)
    if reasons and not force and not verify_token(secret, f"merge:{plan.fingerprint}", plan.fingerprint, confirm_token):
        return MergeResult(
            status="needs_confirmation",
            detail="This merge needs confirmation: " + "; ".join(reasons),
            needs_confirmation=True,
            confirmation_token=issue_token(secret, f"merge:{plan.fingerprint}", plan.fingerprint),
            unmatched=plan.unmatched,
            collisions=plan.collisions,
        )

    if operation_key is None:
        existing = _dedupe(ctx, plan.fingerprint)
        if existing is not None:
            return existing

    # Revalidate both sources against the reviewed snapshots.
    fresh_a = safe_get_playlist(ctx, plan.source_a[0], plan.source_a[1])
    fresh_b = safe_get_playlist(ctx, plan.source_b[0], plan.source_b[1])
    if (
        fresh_a is None
        or fresh_b is None
        or fresh_a.fingerprint() != plan.playlist_a.fingerprint()
        or fresh_b.fingerprint() != plan.playlist_b.fingerprint()
    ):
        return MergeResult(
            status="stale",
            detail="A source playlist changed since the preview; review a fresh preview before merging.",
        )

    if operation_key is None:
        operation_key = ctx.store.create_operation(
            operation_key=plan.fingerprint,
            mapping_id=None,
            direction="merge",
            source_side=plan.source_a[0].value,
            target_side=plan.target_side.value,
            source_playlist_id=f"{plan.source_a[0].value}:{plan.source_a[1]},{plan.source_b[0].value}:{plan.source_b[1]}",
            intended_name=plan.name,
            state=OperationState.PLANNED,
            step="merge_planned",
            source_fingerprint=f"{plan.playlist_a.fingerprint()}+{plan.playlist_b.fingerprint()}",
            desired_fingerprint=fingerprint_ids(plan.desired_ids),
            desired_ids=plan.desired_ids,
        )
    if progress is not None:
        progress.raise_if_cancelled("Cancellation requested before writing; nothing was changed.")
        progress.stage(
            "creating", "Creating destination playlist",
            completed=0, total=len(plan.desired_ids), unit="tracks",
            counters={"planned": len(plan.desired_ids)},
        )
    ctx.store.update_operation(operation_key, state=OperationState.APPLYING, step="creating_destination")
    backend = ctx.backend(plan.target_side)
    try:
        playlist_id = backend.create_playlist(plan.name, plan.desired_ids)
    except Exception as exc:
        from .operations import apply_outcome_to_operation, classify_operation

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
        return MergeResult(
            status="outcome_unknown",
            detail="The merge outcome is not yet known; it will be reconciled. Do not submit again.",
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
            operation_key, state=OperationState.RECOVERY_REQUIRED, step="verify", detail=detail
        )
        return MergeResult(
            status="created_population_incomplete",
            detail=f"A new playlist was created but verification failed: {detail}.",
            playlist_id=playlist_id,
            name=plan.name,
            operation_key=operation_key,
            verification=detail,
            removed=plan.removed,
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
    ctx.store.update_operation(
        operation_key,
        state=OperationState.SUCCEEDED,
        step="verified",
        target_fingerprint_after=read_back.fingerprint(),
    )
    if progress is not None:
        progress.progress(
            completed=len(plan.desired_ids), total=len(plan.desired_ids), unit="entries",
            counters={"confirmed": len(plan.desired_ids), "verified": len(plan.desired_ids)},
            force=True,
        )
    return MergeResult(
        status="merged",
        detail=f"Merge verified ({detail}). Sources were left unchanged.",
        playlist_id=playlist_id,
        name=read_back.summary.name,
        operation_key=operation_key,
        removed=plan.removed,
        final_entries=len(plan.desired_ids),
        verification=detail,
    )


__all__ = [
    "ORDERS",
    "POLICIES",
    "MergePlan",
    "MergeResult",
    "apply_merge",
    "confirmation_reasons",
    "merge_fingerprint",
    "plan_merge",
]
