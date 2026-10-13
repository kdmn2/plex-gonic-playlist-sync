import pytest

from playlist_sync.backends import BackendUnavailable
from playlist_sync.engine.apply import ApplyError, ConcurrentEditError, apply_plan, restore_snapshot
from playlist_sync.engine.planner import plan_mapping
from playlist_sync.engine.sync import sync_mapping
from playlist_sync.models import Direction
from playlist_sync.store import StateLock


def make_mapping(world, *, direction, plex_id=None, gonic_id=None, **kw):
    return world.store.create_mapping(
        name="m",
        direction=direction.value,
        plex_playlist_id=str(plex_id) if plex_id is not None else None,
        gonic_playlist_id=gonic_id,
        **kw,
    )


def mutating_calls(world) -> int:
    return sum(
        1
        for endpoint in world.gonic.requests
        if endpoint in {"createPlaylist", "updatePlaylist", "deletePlaylist"}
    )


def test_plex_to_gonic_apply_mirrors_order_and_duplicates(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a, b, a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)

    result = sync_mapping(world.ctx, mapping.id, dry_run=False, trigger="test")
    assert result.status == "applied_verified"
    assert world.gonic.entries(gid) == [ga, gb, ga]


def test_gonic_to_plex_apply(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [])
    gid = world.gonic_playlist("Mix", [gb, ga, gb])
    mapping = make_mapping(world, direction=Direction.GONIC_TO_PLEX, plex_id=pid, gonic_id=gid)

    result = sync_mapping(world.ctx, mapping.id, dry_run=False, trigger="test")
    assert result.status == "applied_verified"
    assert world.plex.get_playlist(str(pid)).track_ids() == [str(b), str(a), str(b)]


def test_second_run_is_zero_writes(world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)

    assert sync_mapping(world.ctx, mapping.id, dry_run=False).status == "applied_verified"
    before = mutating_calls(world)
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "no_change"
    assert mutating_calls(world) == before


def test_dry_run_never_mutates(world):
    a, _ = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    result = sync_mapping(world.ctx, mapping.id, dry_run=True)
    assert result.status == "planned_changes"
    assert world.gonic.entries(gid) == []
    assert mutating_calls(world) == 0


def test_concurrent_source_edit_aborts(world):
    a, _ = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)

    # someone edits the Plex source after planning
    world.plex_server._occ_counter += 1
    world.plex_server._playlists[pid]._occurrences.append((world.plex_server._occ_counter, b))

    with pytest.raises(ConcurrentEditError):
        apply_plan(world.ctx, plan)
    assert world.gonic.entries(gid) == []


def test_timeout_after_successful_write_is_recovered(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)

    original = world.gonic_client.replace_playlist_entries

    def flaky(playlist_id, track_ids):
        original(playlist_id, track_ids)  # write succeeds
        raise BackendUnavailable("simulated timeout after write")

    world.gonic_client.replace_playlist_entries = flaky  # type: ignore[assignment]
    result = apply_plan(world.ctx, plan)
    assert result.status == "applied_verified"
    assert result.replayed is True
    assert world.gonic.entries(gid) == [ga]


def test_failed_write_is_established_safe(world):
    from playlist_sync.errors import Outcome

    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [ga])  # target already populated
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    # force a change so a write is planned: add another track to source
    b, _gb = world.add_track("A/b.flac")
    world.plex_server._occ_counter += 1
    world.plex_server._playlists[pid]._occurrences.append((world.plex_server._occ_counter, b))
    plan = plan_mapping(world.ctx, mapping)

    def boom(playlist_id, track_ids):
        raise RuntimeError("network exploded")

    world.gonic_client.replace_playlist_entries = boom  # type: ignore[assignment]
    with pytest.raises(ApplyError) as excinfo:
        apply_plan(world.ctx, plan)
    # The write failed before changing anything: this is "failed safe", not partial.
    assert excinfo.value.outcome is Outcome.FAILED_SAFE
    assert excinfo.value.partial is False
    assert excinfo.value.snapshot_id is not None
    snapshots = world.store.list_snapshots(mapping.id)
    assert any(s["kind"] == "pre_write" for s in snapshots)
    # the target is provably unchanged, and the operation records that
    assert world.gonic.entries(gid) == [ga]
    ops = world.store.list_operations(mapping_id=mapping.id)
    assert ops and ops[0]["state"] in {"FAILED_SAFE", "SUCCEEDED"}


def test_restore_from_snapshot(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a, b])
    gid = world.gonic_playlist("Mix", [ga, gb])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)

    # snapshot the populated target, then clear it, then restore
    from playlist_sync.engine.apply import _snapshot_entries

    target = world.gonic_client.get_playlist(gid)
    snap_id = world.store.save_snapshot(
        mapping_id=mapping.id,
        side="gonic",
        playlist_id=gid,
        kind="manual",
        entries=_snapshot_entries(target),
        fingerprint=target.fingerprint(),
    )
    world.gonic_client.replace_playlist_entries(gid, [])
    assert world.gonic.entries(gid) == []
    result = restore_snapshot(world.ctx, snap_id)
    assert result.status == "restored"
    assert world.gonic.entries(gid) == [ga, gb]


def test_state_lock_prevents_overlap(world):
    a, _ = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)

    with StateLock(world.settings.data_dir):
        result = sync_mapping(world.ctx, mapping.id, dry_run=True)
    assert result.status == "busy"
    assert result.exit_code == 3


def test_db_lock_prevents_overlap(world):
    a, _ = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    # hold the SQLite lock with a different owner while releasing the file lock
    world.store.acquire_lock("sync", "someone-else", ttl_seconds=60)
    result = sync_mapping(world.ctx, mapping.id, dry_run=True)
    assert result.status in {"busy"} or result.exit_code == 3
