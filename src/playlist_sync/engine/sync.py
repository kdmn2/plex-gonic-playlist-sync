"""Top-level orchestration shared by CLI, scheduler, and UI."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from ..errors import AppError, Outcome, make_error
from ..models import Direction
from ..store import StateDirectoryBusy, StateLock
from ..store.db import Mapping
from .apply import ApplyError, ConcurrentEditError, apply_plan
from .confirm import confirmation_reasons, issue_token, plan_fingerprint, verify_token
from .context import EngineContext
from .operations import refresh_baseline
from .planner import Plan, plan_mapping
from .reconcile import reconcile_operations

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
    error: AppError | None = None
    extra: dict = field(default_factory=dict)

    def summary(self) -> dict:
        base = {
            "mapping": self.mapping_name,
            "status": self.status,
            "dry_run": self.dry_run,
            "detail": self.detail,
        }
        if self.error is not None:
            base["error"] = self.error.to_dict(include_details=True)
        if self.plan:
            base["plan"] = self.plan.summary()
        if self.extra:
            base["extra"] = self.extra
        return base


def _owner() -> str:
    return f"pid{os.getpid()}-thread{threading.get_ident()}"


def _confirmation_secret(ctx: EngineContext) -> str:
    return ctx.settings.ui.session_secret or ctx.settings.ui.password or "playlist-sync"


def sync_mapping(
    ctx: EngineContext,
    mapping_id: str,
    *,
    dry_run: bool = True,
    trigger: str = "manual",
    acquire_lock: bool = True,
    confirm_token: str | None = None,
    force: bool = False,
) -> SyncResult:
    if not ctx.has_store:
        err = ctx.state_error or make_error("STORAGE_UNAVAILABLE", "The state store is not available.")
        return SyncResult(mapping_id, mapping_id, "storage_problem", err.message, error=err, exit_code=1)

    mapping = ctx.store.get_mapping(mapping_id)
    if mapping is None:
        err = make_error("INTERNAL", "The requested mapping does not exist.")
        return SyncResult(mapping_id, mapping_id, "error", err.message, error=err, exit_code=2)

    if not mapping.enabled or mapping.paused:
        return SyncResult(
            mapping.id,
            mapping.name,
            "paused",
            "This mapping is disabled or paused.",
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
                "Another sync holds the state lock; the run was skipped.",
                dry_run=dry_run,
                exit_code=3,
            )

    try:
        return _sync_locked(
            ctx,
            mapping,
            dry_run=dry_run,
            trigger=trigger,
            confirm_token=confirm_token,
            force=force,
        )
    finally:
        if acquire_lock:
            ctx.store.release_lock("sync", owner)
            for lock in locks:
                lock.release()  # type: ignore[attr-defined]


def _sync_locked(
    ctx: EngineContext,
    mapping: Mapping,
    *,
    dry_run: bool,
    trigger: str,
    confirm_token: str | None,
    force: bool,
) -> SyncResult:
    # Reconcile any unfinished operation for this mapping before planning.
    try:
        reconciled = reconcile_operations(ctx, mapping.id)
    except Exception as exc:
        log.warning("reconcile before sync failed for %s: %s", mapping.name, exc)
        reconciled = []
    unresolved = [
        r for r in reconciled if r.state in {"RECOVERY_REQUIRED", "PARTIALLY_APPLIED"}
    ]
    if unresolved and not dry_run:
        err = make_error(
            "RECOVERY_REQUIRED",
            "An earlier interrupted update for this mapping must be reconciled before syncing again.",
        )
        err.details["operations"] = [r.operation_key for r in unresolved]
        return SyncResult(mapping.id, mapping.name, "recovery_required", err.message, error=err, exit_code=5)

    mode = "dry_run" if dry_run else "apply"
    run_id = ctx.store.start_run(mapping.id, mode, trigger)
    try:
        plan = plan_mapping(ctx, mapping, trigger=trigger)
    except AppError as exc:
        ctx.store.finish_run(run_id, "failed", error=exc.message, error_code=exc.code)
        return SyncResult(mapping.id, mapping.name, "error", exc.message, error=exc, exit_code=1)
    except Exception as exc:
        err = make_error("INTERNAL", "Planning failed.")
        err.details["technical"] = str(exc)
        ctx.store.finish_run(run_id, "failed", error=err.message, error_code=err.code)
        return SyncResult(mapping.id, mapping.name, "error", err.message, error=err, exit_code=1)

    stats = plan.summary()

    if dry_run:
        status = _plan_status(plan)
        ctx.store.finish_run(run_id, status, stats=stats)
        return SyncResult(
            mapping.id, mapping.name, status, "; ".join(plan.blocked_reasons) or "dry run", dry_run=True, plan=plan
        )

    # Storage must be mutable before any remote write.
    try:
        ctx.ensure_mutable()
    except AppError as exc:
        ctx.store.finish_run(run_id, "blocked", stats=stats, error=exc.message, error_code=exc.code)
        return SyncResult(mapping.id, mapping.name, "storage_problem", exc.message, plan=plan, error=exc, exit_code=1)

    if plan.conflict:
        ctx.store.record_conflict(
            mapping.id,
            run_id,
            [{"track_id": t} for t in (plan.source_playlist.track_ids() if plan.source_playlist else [])],
            [{"track_id": t} for t in (plan.target_playlist.track_ids() if plan.target_playlist else [])],
        )
        err = make_error("CONFLICT_DETECTED", "; ".join(plan.blocked_reasons))
        ctx.store.finish_run(run_id, "conflict", stats=stats, error=err.message, error_code=err.code)
        return SyncResult(mapping.id, mapping.name, "conflict", err.message, plan=plan, error=err, exit_code=4)

    if plan.blocked:
        err = _plan_error(plan)
        ctx.store.finish_run(run_id, "blocked", stats=stats, error=err.message, error_code=err.code)
        return SyncResult(mapping.id, mapping.name, "blocked", err.message, plan=plan, error=err, exit_code=5)

    if plan.no_change:
        if Direction(mapping.direction) == Direction.BIDIRECTIONAL:
            refresh_baseline(ctx, mapping)
        ctx.store.finish_run(run_id, "no_change", stats=stats, verified=True)
        detail = "; ".join(plan.notes) or "already in sync"
        return SyncResult(mapping.id, mapping.name, "no_change", detail, plan=plan)

    # Destructive-change confirmation, bound to the reviewed plan.
    fingerprint = plan_fingerprint(plan)
    reasons = confirmation_reasons(plan)
    if reasons and not force and not verify_token(
        _confirmation_secret(ctx), mapping.id, fingerprint, confirm_token
    ):
        codes = [r.code for r in reasons]
        stale = bool(confirm_token)
        prefix = (
            "The plan changed since it was reviewed; here is a fresh preview."
            if stale
            else "This change needs confirmation:"
        )
        detail = f"{prefix} " + "; ".join(r.message for r in reasons)
        ctx.store.finish_run(run_id, "blocked", stats=stats, error=detail, error_code="CONFIRMATION_REQUIRED")
        return SyncResult(
            mapping.id,
            mapping.name,
            "confirmation_required",
            detail,
            plan=plan,
            exit_code=5,
            extra={
                "confirmation_token": issue_token(_confirmation_secret(ctx), mapping.id, fingerprint),
                "plan_fingerprint": fingerprint,
                "reasons": codes,
                "stale": stale,
            },
        )

    try:
        result = apply_plan(ctx, plan, run_id=run_id, trigger=trigger)
    except ConcurrentEditError as exc:
        ctx.store.finish_run(run_id, "aborted", stats=stats, error=exc.message, error_code=exc.code)
        return SyncResult(mapping.id, mapping.name, "aborted", exc.message, plan=plan, error=exc, exit_code=6)
    except ApplyError as exc:
        status_map = {
            Outcome.FAILED_SAFE: "failed_safe",
            Outcome.OUTCOME_UNKNOWN: "outcome_unknown",
            Outcome.PARTIALLY_APPLIED: "partial",
            Outcome.VERIFICATION_FAILED: "verification_failed",
            Outcome.RECOVERY_REQUIRED: "recovery_required",
        }
        status = status_map.get(exc.outcome, "failed")
        ctx.store.finish_run(run_id, status, stats=stats, error=exc.message, error_code=exc.code)
        extra = {"snapshot_id": exc.snapshot_id}
        return SyncResult(
            mapping.id, mapping.name, status, exc.message, plan=plan, error=exc, exit_code=7, extra=extra
        )

    if Direction(mapping.direction) == Direction.BIDIRECTIONAL and result.verified:
        refresh_baseline(ctx, mapping)
    ctx.store.finish_run(run_id, "applied_verified", stats={**stats, "verified": True}, verified=True)
    return SyncResult(
        mapping.id,
        mapping.name,
        "applied_verified",
        result.detail,
        plan=plan,
        applied=True,
        extra={"snapshot_id": result.snapshot_id, "replayed": result.replayed},
    )


def _plan_error(plan: Plan) -> AppError:
    reason = "; ".join(plan.blocked_reasons) or "The plan is blocked."
    if plan.unmatched:
        code = "MATCH_UNMATCHED" if any(u.get("status") == "unmatched" for u in plan.unmatched) else "MATCH_AMBIGUOUS"
    else:
        code = "CAPABILITY_UNSUPPORTED"
    return make_error(code, reason)


def _plan_status(plan: Plan) -> str:
    if plan.conflict:
        return "conflict"
    if plan.blocked:
        return "blocked"
    if plan.no_change:
        return "no_change"
    return "planned_changes"


def sync_all(
    ctx: EngineContext,
    *,
    dry_run: bool = True,
    trigger: str = "manual",
    mapping_ids: list[str] | None = None,
    force: bool = False,
) -> list[SyncResult]:
    """Sync every enabled mapping, isolating failures (IS-10)."""
    if not ctx.has_store:
        err = ctx.state_error or make_error("STORAGE_UNAVAILABLE", "The state store is not available.")
        return [SyncResult("", "", "storage_problem", err.message, error=err, exit_code=1)]
    results: list[SyncResult] = []
    for mapping in ctx.store.list_mappings():
        if mapping_ids is not None and mapping.id not in mapping_ids:
            continue
        if not mapping.enabled:
            continue
        try:
            results.append(
                sync_mapping(ctx, mapping.id, dry_run=dry_run, trigger=trigger, force=force)
            )
        except Exception as exc:
            err = make_error("INTERNAL", "This mapping failed unexpectedly; other mappings were unaffected.")
            err.details["technical"] = f"{type(exc).__name__}: {exc}"
            err.mapping_id = mapping.id
            log.exception("mapping %s failed: %s", mapping.name, exc)
            results.append(
                SyncResult(mapping.id, mapping.name, "error", err.message, error=err, exit_code=1)
            )
    try:
        ctx.store.prune_history(
            runs_keep=ctx.settings.limits.runs_retention,
            snapshots_keep=ctx.settings.limits.snapshots_retention,
        )
    except AppError as exc:
        log.warning("history prune failed: %s", exc)
    return results


def reconcile_startup(ctx: EngineContext) -> list:
    """Reconcile unfinished operations at startup (called by the service)."""
    if not ctx.has_store or not ctx.storage_ready:
        return []
    try:
        return reconcile_operations(ctx)
    except Exception as exc:
        log.warning("startup reconciliation failed: %s", exc)
        return []
