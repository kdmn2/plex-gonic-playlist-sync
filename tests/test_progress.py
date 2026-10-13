import time

from playlist_sync.engine.progress import NullReporter, ProgressReporter
from playlist_sync.store import OperationState


def test_progress_snapshot_revision_and_cancel(world):
    key = world.store.create_operation(
        direction="append", target_side="gonic", state=OperationState.QUEUED, desired_ids=[]
    )
    world.store.operation_progress(
        key, stage="matching", stage_desc="Matching tracks",
        completed=1, total=4, unit="tracks", counters={"matched": 1},
    )
    snapshot = world.store.operation_snapshot(key)
    assert snapshot["completed"] == 1
    assert snapshot["total"] == 4
    assert snapshot["unit"] == "tracks"
    assert snapshot["counters"] == {"matched": 1}
    assert snapshot["revision"] >= 1
    assert snapshot["cancel_available"] is True
    assert snapshot["finished"] is False

    world.store.request_cancel(key)
    assert world.store.operation_snapshot(key)["cancel_available"] is False


def test_reporter_persists_final_state_and_null_reporter(world):
    key = world.store.create_operation(
        direction="append", target_side="gonic", state=OperationState.RUNNING, desired_ids=[]
    )
    reporter = ProgressReporter(world.store, key, min_interval=999)
    reporter.stage("applying", "Adding tracks", completed=0, total=3, unit="tracks")
    reporter.progress(completed=3, total=3, counters={"confirmed": 3}, force=True)
    reporter.finish(OperationState.SUCCEEDED, "done")
    op = world.store.get_operation(key)
    assert op["state"] == OperationState.SUCCEEDED
    assert op["message"] == "done"
    assert NullReporter().cancelled() is False
    NullReporter().finish("SUCCEEDED", "x")  # no-op


def test_background_runner_returns_promptly_and_dedupes(world):
    def worker(reporter):
        reporter.stage("working", "Doing work", completed=0, total=2, unit="steps")
        time.sleep(0.05)
        reporter.progress(completed=2, total=2, counters={"confirmed": 2}, force=True)
        return {"state": OperationState.SUCCEEDED, "message": "done"}

    key = world.ctx.runner.start(
        kind="append", name="test op", worker=worker, submission_key="op-key-1"
    )
    assert key == "op-key-1"
    # returned before completion: a durable row already exists
    assert world.store.get_operation(key) is not None

    operation = {}
    for _ in range(400):
        operation = world.store.get_operation(key)
        if operation["state"] in {OperationState.SUCCEEDED, OperationState.FAILED}:
            break
        time.sleep(0.02)
    assert operation["state"] == OperationState.SUCCEEDED
    assert world.store.operation_snapshot(key)["counters"] == {"confirmed": 2}

    # double submission reuses the same operation, creating no second row
    assert (
        world.ctx.runner.start(kind="append", name="test op", worker=worker, submission_key="op-key-1")
        == key
    )
    rows = [o for o in world.store.list_operations() if o["operation_key"] == key]
    assert len(rows) == 1


def test_live_operation_is_not_reconciled_while_heartbeating(world):
    from playlist_sync.engine.reconcile import reconcile_operations
    from playlist_sync.store.db import utcnow

    key = world.store.create_operation(
        direction="append", target_side="gonic", state=OperationState.RUNNING, desired_ids=[]
    )
    world.store.operation_progress(key, heartbeat_at=utcnow())
    results = reconcile_operations(world.ctx)
    assert all(r.operation_key != key for r in results)
    assert world.store.get_operation(key)["state"] == OperationState.RUNNING
