from playlist_sync.engine.planner import plan_mapping
from playlist_sync.models import Direction, ServerKind


def make_mapping(world, *, direction, plex_id=None, gonic_id=None, **kw):
    return world.store.create_mapping(
        name="m",
        direction=direction.value,
        plex_playlist_id=str(plex_id) if plex_id is not None else None,
        gonic_playlist_id=gonic_id,
        **kw,
    )


def test_directional_plan_lists_additions(world):
    a, ga = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    pid = world.plex_playlist("Source", [a, b])
    gid = world.gonic_playlist("Target", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)
    assert plan.source_side == ServerKind.PLEX
    assert plan.target_side == ServerKind.GONIC
    assert not plan.blocked
    assert len(plan.diff.additions) == 1
    assert plan.desired_ids[-1].startswith("tr-")


def test_unmatched_blocks_by_default(world):
    a, _ = world.add_track("A/a.flac")
    lone, _ = world.add_track("A/lone.flac", plex_only=True)
    pid = world.plex_playlist("Source", [a, lone])
    gid = world.gonic_playlist("Target", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)
    assert plan.blocked
    assert len(plan.unmatched) == 1
    assert plan.unmatched[0]["label"]


def test_partial_sync_opt_in(world):
    a, _ = world.add_track("A/a.flac")
    lone, _ = world.add_track("A/lone.flac", plex_only=True)
    pid = world.plex_playlist("Source", [a, lone])
    gid = world.gonic_playlist("Target", [])
    mapping = make_mapping(
        world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid, allow_partial=True
    )
    plan = plan_mapping(world.ctx, mapping)
    assert not plan.blocked
    assert len(plan.unmatched) == 1
    assert len(plan.desired_ids) == 1


def test_empty_source_does_not_clear_populated_target(world):
    world.add_track("A/a.flac")
    pid = world.plex_playlist("Source", [])
    gonic_a = next(iter(world.gonic.songs.values())).id
    gid = world.gonic_playlist("Target", [gonic_a])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)
    assert plan.blocked
    assert any("empty" in r for r in plan.blocked_reasons)


def test_smart_plex_target_is_blocked(world):
    a, _ = world.add_track("A/a.flac")
    pid = world.plex_playlist("Smart Target", [a], smart=True)
    gid = world.gonic_playlist("Source", [])
    mapping = make_mapping(world, direction=Direction.GONIC_TO_PLEX, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)
    assert plan.blocked
    assert any("smart" in r.lower() for r in plan.blocked_reasons)


def test_missing_source_pauses_mapping(world):
    gid = world.gonic_playlist("Target", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=999, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)
    assert plan.blocked
    refreshed = world.store.get_mapping(mapping.id)
    assert refreshed.paused is True


def test_no_change_when_already_in_sync(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)
    assert plan.no_change
    assert not plan.apply_allowed


def test_rename_detected(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("New Name", [a])
    gid = world.gonic_playlist("Old Name", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    plan = plan_mapping(world.ctx, mapping)
    assert plan.rename_to == "New Name"
    assert plan.apply_allowed
