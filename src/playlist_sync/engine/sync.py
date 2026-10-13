"""Top-level orchestration shared by CLI, scheduler, and UI."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from ..models import Direction, ServerKind
from ..store import StateDirectoryBusy, StateLock
from ..store.db import Mapping
from .apply import ApplyError, ConcurrentEditError, apply_plan
from .context import EngineContext
from .planner import Plan, plan_mapping

log = logging.getLogger("playlist_sync.engine.sync")


@dataclass
class SyncResult:
    mapping_id: str
    mapping_name: str
    status: str
    detail: str = ""
    dry_run: bool = False
    plan: Plan | None = None
    applied: bool = False
    exit_code: int = 0
    extra: dict = field(default_factory=dict)

    def summary(self) -> dict:
        base = {
            "mapping": self.mapping_name,
            "status": self.status,
            "dry_run": self.dry_run,
            "detail": self.detail,
        }
        if self.plan:
            base["plan"] = self.plan.summary()
        return base


def _owner() -> str:
    return f"pid{os.getpid()}-thread{threading.get_ident()}"


def sync_mapping(
    ctx: EngineContext,
    mapping_id: str,
    *,
    dry_run: bool = True,
    trigger: str = "manual",
    acquire_lock: bool = True,
) -> SyncResult:
    mapping = ctx.store.get_mapping(mapping_id)
    if mapping is None:
        return SyncResult(mapping_id, mapping_id, "error", "mapping not found", exit_code=2)

    if not mapping.enabled or mapping.paused:
        return SyncResult(
            mapping.id,
            mapping.name,
            "paused",
            "mapping is disabled or paused",
            dry_run=dry_run,
            exit_code=0,
        )

    owner = _owner()
    locks: list[object] = []
    if acquire_lock:
        state_lock = StateLock(ctx.settings.data_dir)
        try:
            state_lock.acquire()
            locks.append(state_lock)
        except StateDirectoryBusy as exc:
            return SyncResult(
                mapping.id, mapping.name, "busy", str(exc), dry_run=dry_run, exit_code=3
            )
        if not ctx.store.acquire_lock("sync", owner, ttl_seconds=1800):
            state_lock.release()
            return SyncResult(
                mapping.id,
                mapping.name,
                "busy",
                "another sync holds the state lock",
                dry_run=dry_run,
                exit_code=3,
            )

    try:
        return _sync_locked(ctx, mapping, dry_run=dry_run, trigger=trigger)
    finally:
        if acquire_lock:
            ctx.store.release_lock("sync", owner)
            for lock in locks:
                lock.release()  # type: ignore[attr-defined]


def _sync_locked(
    ctx: EngineContext, mapping: Mapping, *, dry_run: bool, trigger: str
) -> SyncResult:
    mode = "dry_run" if dry_run else "apply"
    run_id = ctx.store.start_run(mapping.id, mode, trigger)
    try:
        plan = plan_mapping(ctx, mapping, trigger=trigger)
    except Exception as exc:
        ctx.store.finish_run(run_id, "failed", error=str(exc))
        return SyncResult(
            mapping.id, mapping.name, "error", f"planning failed: {exc}", dry_run=dry_run, exit_code=1
        )

    stats = plan.summary()
    if dry_run:
        status = _plan_status(plan)
        ctx.store.finish_run(run_id, status, stats=stats)
        return SyncResult(
            mapping.id, mapping.name, status, "; ".join(plan.blocked_reasons) or "dry run", dry_run=True, plan=plan
        )

    if plan.conflict:
        ctx.store.record_conflict(
            mapping.id,
            run_id,
            [{"track_id": t} for t in (plan.source_playlist.track_ids() if plan.source_playlist else [])],
            [{"track_id": t} for t in (plan.target_playlist.track_ids() if plan.target_playlist else [])],
        )
        ctx.store.finish_run(run_id, "conflict", stats=stats)
        return SyncResult(
            mapping.id, mapping.name, "conflict", "; ".join(plan.blocked_reasons), plan=plan, exit_code=4
        )

    if plan.blocked:
        ctx.store.finish_run(run_id, "blocked", stats=stats)
        return SyncResult(
            mapping.id, mapping.name, "blocked", "; ".join(plan.blocked_reasons), plan=plan, exit_code=5
        )

    if plan.no_change:
        # Bidirectional "changed identically" and bootstrap refresh the baseline.
        if Direction(mapping.direction) == Direction.BIDIRECTIONAL:
            refresh_baseline(ctx, mapping)
        ctx.store.finish_run(run_id, "no_change", stats=stats)
        detail = "; ".join(plan.notes) or "already in sync"
        return SyncResult(mapping.id, mapping.name, "no_change", detail, plan=plan)

    try:
        result = apply_plan(ctx, plan, run_id=run_id, trigger=trigger)
    except ConcurrentEditError as exc:
        ctx.store.finish_run(run_id, "aborted", stats=stats, error=str(exc))
        return SyncResult(mapping.id, mapping.name, "aborted", str(exc), plan=plan, exit_code=6)
    except ApplyError as exc:
        ctx.store.finish_run(run_id, "failed", stats=stats, error=str(exc))
        return SyncResult(
            mapping.id,
            mapping.name,
            "partial" if exc.partial else "failed",
            str(exc),
            plan=plan,
            exit_code=7,
            extra={"snapshot_id": exc.snapshot_id},
        )

    if Direction(mapping.direction) == Direction.BIDIRECTIONAL:
        refresh_baseline(ctx, mapping)
    ctx.store.finish_run(run_id, "applied_verified", stats={**stats, "verified": True})
    return SyncResult(
        mapping.id,
        mapping.name,
        "applied_verified",
        result.detail,
        plan=plan,
        applied=True,
        extra={"snapshot_id": result.snapshot_id, "replayed": result.replayed},
    )


def _plan_status(plan: Plan) -> str:
    if plan.conflict:
        return "conflict"
    if plan.blocked:
        return "blocked"
    if plan.no_change:
        return "no_change"
    return "planned_changes"


def refresh_baseline(ctx: EngineContext, mapping: Mapping) -> None:
    """Persist the current both-sides state as the new bidirectional baseline."""
    from ..matching.matcher import TrackMatcher

    plex_pl = _safe_get(ctx, ServerKind.PLEX, mapping.plex_playlist_id)
    gonic_pl = _safe_get(ctx, ServerKind.GONIC, mapping.gonic_playlist_id)
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


def _safe_get(ctx: EngineContext, side: ServerKind, playlist_id: str | None):
    from ..backends import PlaylistNotFound

    if not playlist_id:
        return None
    try:
        return ctx.backend(side).get_playlist(playlist_id)
    except PlaylistNotFound:
        return None


def sync_all(
    ctx: EngineContext,
    *,
    dry_run: bool = True,
    trigger: str = "manual",
    mapping_ids: list[str] | None = None,
) -> list[SyncResult]:
    results: list[SyncResult] = []
    for mapping in ctx.store.list_mappings():
        if mapping_ids is not None and mapping.id not in mapping_ids:
            continue
        if not mapping.enabled:
            continue
        results.append(sync_mapping(ctx, mapping.id, dry_run=dry_run, trigger=trigger))
    return results
