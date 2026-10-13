import pytest

from playlist_sync.backends import BackendAuthError, BackendUnavailable
from playlist_sync.engine.apply import ApplyError, apply_plan
from playlist_sync.engine.planner import plan_mapping
from playlist_sync.engine.sync import sync_all, sync_mapping
from playlist_sync.errors import AppError, Outcome, make_error
from playlist_sync.models import Direction, fingerprint_ids


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
        for e in world.gonic.requests
        if e in {"createPlaylist", "updatePlaylist", "deletePlaylist"}
    )


def test_backend_unavailable_before_planning_leaves_state_untouched(world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)

    def boom():
        raise BackendUnavailable("gonic is down", code="CONNECT_REFUSED")

    world.gonic_client.iter_library_tracks = boom  # type: ignore[assignment]
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "error"
    assert result.error is not None and result.error.category.value == "connectivity"
    assert world.gonic.entries(gid) == []
    assert mutating_calls(world) == 0
    assert world.store.list_active_operations() == []


def test_expired_credentials_are_reported_as_authentication(world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)

    def boom():
        raise BackendAuthError("expired")

    world.gonic_client.iter_library_tracks = boom  # type: ignore[assignment]
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "error"
    assert result.error.category.value == "authentication"
    assert mutating_calls(world) == 0


def test_storage_problem_blocks_writes(world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    world.ctx.state_error = make_error("STORAGE_CORRUPT", "corrupt state")
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "storage_problem"
    assert mutating_calls(world) == 0


def test_journal_write_failure_prevents_remote_write(world):
    a, ga = world.add_track("A/a.flac")
    b, _gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a, b])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)

    def boom(**kwargs):
        raise make_error("STORAGE_FULL", "disk full")

    world.store.create_operation = boom  # type: ignore[assignment]
    with pytest.raises(AppError):
        apply_plan(world.ctx, plan)
    assert mutating_calls(world) == 0
    assert world.gonic.entries(gid) == [ga]


def test_timeout_before_write_is_failed_safe(world):
    a, ga = world.add_track("A/a.flac")
    b, _gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a, b])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)

    def boom(playlist_id, track_ids):
        raise BackendUnavailable("timeout before applying")

    world.gonic_client.replace_playlist_entries = boom  # type: ignore[assignment]
    with pytest.raises(ApplyError) as excinfo:
        apply_plan(world.ctx, plan)
    assert excinfo.value.outcome is Outcome.FAILED_SAFE
    assert world.gonic.entries(gid) == [ga]


def test_partial_application_requires_recovery(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [])
    gid = world.gonic_playlist("Mix", [ga, gb])
    mapping = make_mapping(world, direction=Direction.GONIC_TO_PLEX, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)

    def partial(playlist_id, track_ids):
        ids = [str(x) for x in track_ids]
        target = world.plex._playlist_obj(playlist_id)
        world.plex._append_ids(target, ids[:1])  # apply only the first of several
        raise RuntimeError("connection dropped mid-apply")

    world.plex.replace_playlist_entries = partial  # type: ignore[assignment]
    with pytest.raises(ApplyError) as excinfo:
        apply_plan(world.ctx, plan)
    assert excinfo.value.outcome is Outcome.RECOVERY_REQUIRED
    assert world.store.get_mapping(mapping.id).paused is True
    assert any(s["kind"] == "pre_write" for s in world.store.list_snapshots(mapping.id))


def test_missing_track_during_restore_blocks(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    gid = world.gonic_playlist("Mix", [ga, gb])
    snap = world.store.save_snapshot(
        mapping_id="m", side="gonic", playlist_id=gid, kind="manual",
        entries=[{"track_id": ga}, {"track_id": gb}], fingerprint=fingerprint_ids([ga, gb]),
    )
    # gb disappears from the library
    world.gonic.songs.pop(gb, None)
    for tracks in world.gonic.albums.values():
        if gb in tracks:
            tracks.remove(gb)
    from playlist_sync.engine.apply import restore_snapshot

    with pytest.raises(ApplyError) as excinfo:
        restore_snapshot(world.ctx, snap)
    assert excinfo.value.code == "RESTORE_UNSAFE"
    assert world.gonic.entries(gid) == [ga, gb]  # untouched


def test_one_broken_mapping_does_not_affect_others(world):
    a, ga = world.add_track("A/a.flac")
    good_plex = world.plex_playlist("Good", [a])
    good_gonic = world.gonic_playlist("Good", [])
    good = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=good_plex, gonic_id=good_gonic)
    broken = make_mapping(
        world, direction=Direction.PLEX_TO_GONIC, plex_id=999999, gonic_id=world.gonic_playlist("Broken", [])
    )

    results = sync_all(world.ctx, dry_run=False)
    by_id = {r.mapping_id: r for r in results}
    assert by_id[good.id].status == "applied_verified"
    assert world.gonic.entries(good_gonic) == [ga]
    assert by_id[broken.id].status in {"blocked", "paused", "error"}


def test_lock_wait_is_bounded(world):
    assert world.store.acquire_lock("sync", "owner-a", ttl_seconds=60)
    assert world.store.acquire_lock("sync", "owner-b", ttl_seconds=60, wait_seconds=0.2) is False
