from playlist_sync.engine.append import apply_append, plan_append
from playlist_sync.models import ServerKind

SECRET = "append-secret"


def _apply(world, plan, **kw):
    return apply_append(world.ctx, plan, secret=SECRET, **kw)


def _gonic_id(world, path):
    return next(s.id for s in world.gonic.songs.values() if s.path == path)


def test_plex_to_plex_preserves_destination_prefix(world):
    a, _ = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    src = world.plex_playlist("Src", [a, b])
    dst = world.plex_playlist("Dst", [b])
    plan = plan_append(world.ctx, ServerKind.PLEX, str(src), ServerKind.PLEX, str(dst))
    assert plan.additions == [str(a)]
    result = _apply(world, plan)
    assert result.status == "appended"
    assert world.plex.get_playlist(str(dst)).track_ids() == [str(b), str(a)]
    assert world.plex.get_playlist(str(src)).track_ids() == [str(a), str(b)]


def test_gonic_to_gonic(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    src = world.gonic_playlist("Src", [ga, gb])
    dst = world.gonic_playlist("Dst", [ga])
    plan = plan_append(world.ctx, ServerKind.GONIC, src, ServerKind.GONIC, dst)
    assert plan.additions == [gb]
    _apply(world, plan)
    assert world.gonic.entries(dst) == [ga, gb]


def test_plex_to_gonic_cross_server(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    src = world.plex_playlist("Src", [a, b])
    dst = world.gonic_playlist("Dst", [ga])
    plan = plan_append(world.ctx, ServerKind.PLEX, str(src), ServerKind.GONIC, dst)
    assert plan.additions == [gb]
    _apply(world, plan)
    assert world.gonic.entries(dst) == [ga, gb]


def test_gonic_to_plex_cross_server(world):
    a, _ = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    ga = _gonic_id(world, "A/a.flac")
    gb = _gonic_id(world, "A/b.flac")
    src = world.gonic_playlist("Src", [ga, gb])
    dst = world.plex_playlist("Dst", [a])
    plan = plan_append(world.ctx, ServerKind.GONIC, src, ServerKind.PLEX, str(dst))
    assert plan.additions == [str(b)]
    _apply(world, plan)
    assert world.plex.get_playlist(str(dst)).track_ids() == [str(a), str(b)]


def test_source_duplicates_add_at_most_once(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    src = world.gonic_playlist("Src", [ga, ga, gb])
    dst = world.gonic_playlist("Dst", [])
    plan = plan_append(world.ctx, ServerKind.GONIC, src, ServerKind.GONIC, dst)
    assert plan.additions == [ga, gb]
    assert plan.skipped_repeat == 1


def test_existing_destination_duplicates_unchanged(world):
    _b, gb = world.add_track("A/b.flac")
    src = world.gonic_playlist("Src", [gb])
    dst = world.gonic_playlist("Dst", [gb, gb])
    plan = plan_append(world.ctx, ServerKind.GONIC, src, ServerKind.GONIC, dst)
    assert plan.additions == []
    assert _apply(world, plan).status == "no_change"
    assert world.gonic.entries(dst) == [gb, gb]


def test_distinct_recordings_not_deduplicated(world):
    a, _ = world.add_track("A/song.flac", title="Song", album="X")
    b, _ = world.add_track("B/song.flac", title="Song", album="X")
    src = world.plex_playlist("Src", [b])
    dst = world.plex_playlist("Dst", [a])
    plan = plan_append(world.ctx, ServerKind.PLEX, str(src), ServerKind.PLEX, str(dst))
    assert plan.additions == [str(b)]


def test_missing_blocks_or_partial(world):
    a, _ = world.add_track("A/a.flac")
    lone, _ = world.add_track("A/lone.flac", plex_only=True)
    src = world.plex_playlist("Src", [a, lone])
    dst = world.gonic_playlist("Dst", [])
    blocked = plan_append(world.ctx, ServerKind.PLEX, str(src), ServerKind.GONIC, dst)
    assert blocked.blocked
    assert _apply(world, blocked).status == "blocked"
    partial = plan_append(
        world.ctx, ServerKind.PLEX, str(src), ServerKind.GONIC, dst, allow_partial=True
    )
    assert not partial.blocked
    assert len(partial.additions) == 1


def test_same_source_and_destination_blocked(world):
    _, ga = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Same", [ga])
    plan = plan_append(world.ctx, ServerKind.GONIC, gid, ServerKind.GONIC, gid)
    assert plan.blocked


def test_smart_plex_destination_blocked(world):
    a, _ = world.add_track("A/a.flac")
    src = world.plex_playlist("Src", [a])
    smart = world.plex_playlist("Smart", [a], smart=True)
    plan = plan_append(world.ctx, ServerKind.PLEX, str(src), ServerKind.PLEX, str(smart))
    assert plan.blocked
    assert any("smart" in r.lower() for r in plan.blocked_reasons)


def test_destination_changed_after_preview_is_stale(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    src = world.gonic_playlist("Src", [gb])
    dst = world.gonic_playlist("Dst", [ga])
    plan = plan_append(world.ctx, ServerKind.GONIC, src, ServerKind.GONIC, dst)
    world.gonic.playlists[dst].entries.append(ga)
    assert _apply(world, plan).status == "stale"


def test_repeated_execution_is_noop(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    src = world.gonic_playlist("Src", [ga, gb])
    dst = world.gonic_playlist("Dst", [ga])
    plan = plan_append(world.ctx, ServerKind.GONIC, src, ServerKind.GONIC, dst)
    assert _apply(world, plan).status == "appended"
    before = len(world.gonic.requests)
    plan2 = plan_append(world.ctx, ServerKind.GONIC, src, ServerKind.GONIC, dst)
    assert plan2.additions == []
    assert _apply(world, plan2).status == "no_change"
    mutating = [r for r in world.gonic.requests[before:] if r in {"createPlaylist", "updatePlaylist"}]
    assert mutating == []


def test_smart_source_is_read_only(world):
    a, _ = world.add_track("A/a.flac")
    smart = world.plex_playlist("Smart", [a], smart=True)
    dst = world.gonic_playlist("Dst", [])
    before = list(world.plex_server._playlists[smart]._occurrences)
    plan = plan_append(world.ctx, ServerKind.PLEX, str(smart), ServerKind.GONIC, dst)
    _apply(world, plan)
    assert world.plex_server._playlists[smart]._occurrences == before
