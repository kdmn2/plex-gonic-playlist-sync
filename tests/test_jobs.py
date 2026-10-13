from datetime import UTC, datetime, timedelta

import pytest

from playlist_sync.engine import jobs
from playlist_sync.web.job_scheduler import JobScheduler


def test_validate_frequency_rules():
    assert jobs.validate_frequency({"kind": "manual"}) == []
    assert jobs.validate_frequency({"kind": "minutes", "n": 2})  # below minimum
    assert jobs.validate_frequency({"kind": "hours", "n": 6}) == []
    assert jobs.validate_frequency({"kind": "daily", "time": "nope"})
    assert jobs.validate_frequency({"kind": "weekly", "time": "03:00", "days": []})
    assert jobs.validate_frequency({"kind": "weekly", "time": "03:00", "days": [0, 3]}) == []


def test_describe_frequency():
    assert "Manual" in jobs.describe_frequency({"kind": "manual"})
    assert jobs.describe_frequency({"kind": "hours", "n": 6})
    assert "Daily at 03:00" in jobs.describe_frequency({"kind": "daily", "time": "03:00"}, "UTC")


def test_interval_next_run_is_anchored():
    after = datetime(2026, 1, 1, 7, 0, tzinfo=UTC)
    assert jobs.compute_next_run({"kind": "hours", "n": 6}, "UTC", after) == datetime(
        2026, 1, 1, 12, 0, tzinfo=UTC
    )


def test_daily_and_weekly_next_run():
    after = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)  # Monday
    daily = jobs.compute_next_run({"kind": "daily", "time": "03:00"}, "UTC", after)
    assert daily == datetime(2026, 6, 2, 3, 0, tzinfo=UTC)
    weekly = jobs.compute_next_run({"kind": "weekly", "time": "03:00", "days": [2]}, "UTC", after)
    assert weekly == datetime(2026, 6, 3, 3, 0, tzinfo=UTC)


def test_dst_spring_forward_nonexistent_time():
    after = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)  # UK 01:00 -> 02:00 local
    nxt = jobs.compute_next_run({"kind": "daily", "time": "01:30"}, "Europe/London", after)
    assert nxt == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 02:00 BST


def test_dst_fall_back_repeated_time_executes_once():
    after = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    first = jobs.compute_next_run({"kind": "daily", "time": "01:30"}, "Europe/London", after)
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # first (BST) occurrence
    second = jobs.compute_next_run({"kind": "daily", "time": "01:30"}, "Europe/London", first)
    assert second == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)  # next day, not the repeated hour


def test_occurrence_id_is_deterministic():
    when = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert jobs.occurrence_id("j1", when) == jobs.occurrence_id("j1", when)


def test_build_job_and_crud(world):
    job = jobs.build_job(
        world.ctx, name="nightly", type="append",
        params={"source_side": "gonic", "source_id": "a", "dest_side": "gonic", "dest_id": "b"},
        frequency={"kind": "hours", "n": 6}, timezone="UTC",
    )
    assert job["next_run"] is not None
    assert world.store.get_job(job["id"])["name"] == "nightly"
    assert jobs.reschedule_job(
        world.ctx, job["id"], frequency={"kind": "hours", "n": 2}
    ) is not None


def test_build_job_rejects_bad_frequency(world):
    from playlist_sync.errors import AppError

    with pytest.raises(AppError):
        jobs.build_job(world.ctx, name="x", type="append", frequency={"kind": "minutes", "n": 1})


async def test_scheduler_runs_due_append_job(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    src = world.gonic_playlist("Src", [ga, gb])
    dst = world.gonic_playlist("Dst", [ga])
    job = jobs.build_job(
        world.ctx, name="append-run", type="append",
        params={"source_side": "gonic", "source_id": src, "dest_side": "gonic", "dest_id": dst},
        frequency={"kind": "hours", "n": 6},
    )
    world.store.update_job(job["id"], next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    scheduler = JobScheduler(world.ctx, poll_seconds=30)
    outcomes = await scheduler.tick_once()
    assert any(o["status"] == "appended" for o in outcomes)
    assert world.gonic.entries(dst) == [ga, gb]
    assert world.store.get_job(job["id"])["last_success"] is not None
    # a run row was recorded
    assert any(r["mapping_id"] == f"job:{job['id']}" for r in world.store.list_runs(limit=5))


async def test_scheduler_coalesces_when_executor_busy(world):
    _, _ga = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Dst", [])
    job = jobs.build_job(
        world.ctx, name="busy", type="append",
        params={"source_side": "gonic", "source_id": gid, "dest_side": "gonic", "dest_id": gid},
        frequency={"kind": "hours", "n": 6},
    )
    world.store.update_job(job["id"], next_run=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    assert world.store.acquire_lock("sync", "someone-else", ttl_seconds=60)
    scheduler = JobScheduler(world.ctx)
    outcomes = await scheduler.tick_once()
    assert outcomes and outcomes[0]["status"] == "coalesced"
    # lock not taken and job advanced past the missed occurrence
    assert world.store.lock_owner("sync") == "someone-else"
    world.store.release_lock("sync", "someone-else")


async def test_failed_job_does_not_stop_others(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    src = world.gonic_playlist("Src", [ga, gb])
    dst = world.gonic_playlist("Dst", [ga])
    bad = jobs.build_job(world.ctx, name="bad", type="nonsense", params={}, frequency={"kind": "hours", "n": 6})
    good = jobs.build_job(
        world.ctx, name="good", type="append",
        params={"source_side": "gonic", "source_id": src, "dest_side": "gonic", "dest_id": dst},
        frequency={"kind": "hours", "n": 6},
    )
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    world.store.update_job(bad["id"], next_run=past)
    world.store.update_job(good["id"], next_run=past)
    outcomes = await JobScheduler(world.ctx).tick_once()
    statuses = {o["job"]: o["status"] for o in outcomes}
    assert statuses[bad["id"]] == "failed"
    assert statuses[good["id"]] == "appended"
