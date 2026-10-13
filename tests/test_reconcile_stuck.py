"""Regression tests: a stuck/orphaned operation must not be reported as needing
reconciliation while the reconcile button then says "nothing to reconcile".

Root cause was a mismatch: the dashboard counted *every* non-terminal operation as
"unfinished" while the reconciler skipped live, heartbeating ones -- including an
operation orphaned by a restart (its heartbeat was still within the 120s window).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from playlist_sync.engine.context import EngineContext
from playlist_sync.engine.health import mapping_health, overview
from playlist_sync.engine.operations import classify_operation
from playlist_sync.engine.reconcile import (
    operation_is_running,
    operation_needs_reconciliation,
    reconcile_operations,
    running_operations,
)
from playlist_sync.store import OperationState


def _old_stamp(seconds: int = 600) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _backup_op(world, *, state=OperationState.RUNNING, heartbeat=None, mapping_id=None):
    key = world.store.create_operation(
        mapping_id=mapping_id,
        direction="backup",
        intended_name="Backup 32 playlists",
        state=state,
        step="backing_up",
    )
    if heartbeat is not None:
        # heartbeat_at is written by the progress API (update_operation ignores it).
        world.store.operation_progress(key, heartbeat_at=heartbeat)
    return key


def _restart(world) -> EngineContext:
    return EngineContext(
        settings=world.settings, store=world.store, path_mapper=world.path_mapper,
        plex=world.plex, gonic=world.gonic_client,
    )


def test_backup_classifies_as_failed_safe(world):
    key = _backup_op(world)
    op = world.store.get_operation(key)
    outcome = classify_operation(world.ctx, op)
    assert outcome.state == OperationState.FAILED_SAFE


def test_orphaned_backup_after_restart_is_reconciled_not_skipped(world):
    # An operation left behind by a *previous* process: heartbeat predates this
    # process start, so it must be reconcilable even though it is < 120s old.
    key = _backup_op(world, heartbeat=_old_stamp(seconds=30))
    ctx2 = _restart(world)

    op = world.store.get_operation(key)
    assert operation_is_running(ctx2, op) is False
    assert operation_needs_reconciliation(ctx2, op) is True

    results = reconcile_operations(ctx2)
    assert [r.operation_key for r in results] == [key]
    assert results[0].state == OperationState.FAILED_SAFE
    # terminal now -> no longer reported as unfinished
    assert world.store.list_active_operations() == []


def test_restart_reconciles_immediately_before_the_old_grace_window(world):
    # Exactly the user's case: a container restarted mid-backup. Even though the
    # last heartbeat is only seconds old, no worker in this process owns it.
    key = _backup_op(world, heartbeat=_old_stamp(seconds=5))
    ctx2 = _restart(world)
    results = reconcile_operations(ctx2)
    assert results and results[0].operation_key == key
    assert overview(ctx2)["active_operations"] == []


def test_operation_owned_by_this_process_is_running_not_unfinished(world):
    # Created now by this process -> heartbeat is after process_started_at.
    key = _backup_op(world)
    op = world.store.get_operation(key)
    assert operation_is_running(world.ctx, op) is True
    assert operation_needs_reconciliation(world.ctx, op) is False
    # reconcile must not touch a running worker
    assert reconcile_operations(world.ctx) == []
    assert [o["operation_key"] for o in running_operations(world.ctx)] == [key]


def test_health_counts_agree_with_reconcile(world):
    key = _backup_op(world)
    health = overview(world.ctx)
    # running operation: shown as running, NOT as needing reconciliation
    assert [o["operation_key"] for o in health["running_operations"]] == [key]
    assert health["active_operations"] == []

    # after a restart it becomes pending, and the count matches reconcile
    ctx2 = _restart(world)
    health2 = overview(ctx2)
    assert [o["operation_key"] for o in health2["active_operations"]] == [key]
    assert reconcile_operations(ctx2)  # non-empty -> button will report it


def test_mapping_health_says_in_progress_vs_needs_reconciliation(world):
    mapping = world.store.create_mapping(name="m", direction="plex_to_gonic")
    key = world.store.create_operation(
        mapping_id=mapping.id, direction="plex_to_gonic", state=OperationState.RUNNING, step="running"
    )
    health = mapping_health(world.ctx, world.store.get_mapping(mapping.id))
    assert health["state"] == "running"
    assert "in progress" in health["detail"]

    ctx2 = _restart(world)
    health2 = mapping_health(ctx2, world.store.get_mapping(mapping.id))
    assert health2["state"] == "recovery_required"
    assert "need reconciliation" in health2["detail"]
    assert key  # referenced to keep intent explicit
