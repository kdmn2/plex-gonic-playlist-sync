"""Reconcile unfinished operations.

Runs on service startup and before any mapping is synced. It re-reads remote
state, classifies what each interrupted operation actually did, and updates the
operation and mapping accordingly. It never blindly replays a mutation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..models import Direction
from ..store import OperationState
from ..store.db import ACTIVE_OPERATION_STATES, LIVE_OPERATION_STATES
from .operations import apply_outcome_to_operation, classify_operation, refresh_baseline

log = logging.getLogger("playlist_sync.engine.reconcile")


def list_operations_needing_reconciliation(ctx) -> list[dict]:
    """Operations the reconciler will act on (excludes live, heartbeating work)."""
    if not ctx.has_store:
        return []
    return [op for op in ctx.store.list_active_operations() if operation_needs_reconciliation(ctx, op)]


def operation_is_running(ctx, op: dict) -> bool:
    """True only for a live operation *this process* is actually working on.

    A persisted operation in a live state is treated as running only when a
    worker is registered in-process, or its heartbeat was written after this
    process started. An operation left behind by a previous process (a restart
    mid-backup) has an older heartbeat and is therefore reconcilable -- the old
    time-window check wrongly skipped it for the first two minutes.
    """
    state = op.get("state")
    if state not in LIVE_OPERATION_STATES:
        return False
    key = op.get("operation_key")
    # A registered background worker is definitive.
    try:
        if key and key in ctx.runner.running():
            return True
    except Exception:
        pass
    stamp = op.get("heartbeat_at") or op.get("updated_at")
    started = getattr(ctx, "process_started_at", None)
    if not stamp or not started:
        return False
    return stamp >= started


def operation_needs_reconciliation(ctx, op: dict) -> bool:
    if op.get("state") not in ACTIVE_OPERATION_STATES:
        return False
    return not operation_is_running(ctx, op)


def running_operations(ctx) -> list[dict]:
    """Active operations a live worker in this process is still working on."""
    if not ctx.has_store:
        return []
    return [op for op in ctx.store.list_active_operations() if operation_is_running(ctx, op)]


@dataclass
class ReconcileResult:
    operation_key: str
    mapping_id: str | None
    state: str
    detail: str
    needs_intervention: bool


def reconcile_operations(ctx, mapping_id: str | None = None) -> list[ReconcileResult]:
    if not ctx.has_store or not ctx.storage_ready:
        return []
    operations = ctx.store.list_active_operations()
    if mapping_id is not None:
        operations = [op for op in operations if op["mapping_id"] == mapping_id]

    results: list[ReconcileResult] = []
    for op in operations:
        if operation_is_running(ctx, op):
            # A live worker in this process is heartbeating; do not reconcile it.
            continue
        try:
            outcome = classify_operation(ctx, op)
        except Exception as exc:
            log.warning(
                "reconcile: could not classify operation %s: %s", op["operation_key"], exc
            )
            ctx.store.update_operation(
                op["operation_key"],
                state=OperationState.RECOVERY_REQUIRED,
                step="reconcile_failed",
                detail="reconciliation could not reach the backend; will retry",
            )
            results.append(
                ReconcileResult(op["operation_key"], op["mapping_id"], OperationState.RECOVERY_REQUIRED, "reconciliation pending", True)
            )
            continue

        apply_outcome_to_operation(ctx, op, outcome)
        if outcome.state == OperationState.SUCCEEDED:
            mapping = ctx.store.get_mapping(op["mapping_id"]) if op["mapping_id"] else None
            if mapping is not None and Direction(mapping.direction) == Direction.BIDIRECTIONAL:
                refresh_baseline(ctx, mapping)
            log.info(
                "reconcile: operation %s recovered as %s (%s)",
                op["operation_key"],
                outcome.state,
                outcome.detail,
            )
        elif outcome.state in {OperationState.RECOVERY_REQUIRED, OperationState.PARTIALLY_APPLIED}:
            log.warning(
                "reconcile: operation %s needs intervention: %s",
                op["operation_key"],
                outcome.detail,
            )
        results.append(
            ReconcileResult(
                op["operation_key"], op["mapping_id"], outcome.state, outcome.detail, outcome.needs_intervention
            )
        )
    return results
