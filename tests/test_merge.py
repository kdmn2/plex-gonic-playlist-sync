from playlist_sync.engine.merge import apply_merge, plan_merge
from playlist_sync.models import ServerKind

SECRET = "merge-secret"


def _merge(world, a, b, target, name, **kw):
    plan = plan_merge(world.ctx, a, b, target, name, **kw)
    return plan, apply_merge(world.ctx, plan, secret=SECRET, **{k: v for k, v in kw.items() if k == "confirm_token"})


def test_plex_plex_duplicate_policies(world):
    a, _ = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    c, _ = world.add_track("A/c.flac")
    pa = world.plex_playlist("A", [a, b])
    pb = world.plex_playlist("B", [b, c])
    before_a = world.plex.get_playlist(str(pa)).track_ids()
    before_b = world.plex.get_playlist(str(pb)).track_ids()

    for policy, expected in (
        ("keep_all", [str(a), str(b), str(b), str(c)]),
        ("remove_overlaps", [str(a), str(b), str(c)]),
        ("first_occurrence", [str(a), str(b), str(c)]),
    ):
        plan = plan_merge(
            world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.PLEX, str(pb)), ServerKind.PLEX, f"m-{policy}",
            order="ab", policy=policy,
        )
        assert not plan.blocked, plan.blocked_reasons
        result = apply_merge(world.ctx, plan, secret=SECRET)
        assert result.status == "merged"
        assert world.plex.get_playlist(result.playlist_id).track_ids() == expected

    # sources unchanged
    assert world.plex.get_playlist(str(pa)).track_ids() == before_a
    assert world.plex.get_playlist(str(pb)).track_ids() == before_b


def test_order_reversed(world):
    a, _ = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    pa = world.plex_playlist("A", [a])
    pb = world.plex_playlist("B", [b])
    plan = plan_merge(
        world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.PLEX, str(pb)), ServerKind.GONIC, "rev", order="ba"
    )
    result = apply_merge(world.ctx, plan, secret=SECRET)
    # B first: gonic ids for b then a; verify first resolved entry corresponds to b
    table = {t.path: t.id for t in world.gonic.songs.values()}
    assert world.gonic.entries(result.playlist_id)[0] == table["A/b.flac"]


def test_cross_server_and_remove_overlaps(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    _c, gc = world.add_track("A/c.flac")
    pa = world.plex_playlist("A", [a, b])
    pb = world.gonic_playlist("B", [gb, gc])  # overlaps on b
    plan = plan_merge(
        world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.GONIC, pb), ServerKind.GONIC, "cross",
        order="ab", policy="remove_overlaps",
    )
    result = apply_merge(world.ctx, plan, secret=SECRET)
    # A resolves to ga,gb; B keeps gc only (gb overlaps)
    assert world.gonic.entries(result.playlist_id) == [ga, gb, gc]


def test_gonic_gonic_identity_and_duplicates(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    g1 = world.gonic_playlist("G1", [ga, ga])
    g2 = world.gonic_playlist("G2", [gb, ga])
    plan = plan_merge(
        world.ctx, (ServerKind.GONIC, g1), (ServerKind.GONIC, g2), ServerKind.GONIC, "gg",
        order="ab", policy="keep_all",
    )
    result = apply_merge(world.ctx, plan, secret=SECRET)
    assert world.gonic.entries(result.playlist_id) == [ga, ga, gb, ga]


def test_distinct_recordings_remain_distinct(world):
    a, ga = world.add_track("A/song.flac", title="Song", album="X")
    # different file path -> distinct track even with same metadata title
    b, gb = world.add_track("B/song.flac", title="Song", album="X")
    pa = world.plex_playlist("A", [a])
    pb = world.plex_playlist("B", [b])
    plan = plan_merge(
        world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.PLEX, str(pb)), ServerKind.GONIC, "distinct",
        policy="remove_overlaps",
    )
    assert plan.desired_ids == [ga, gb]


def test_empty_sources(world):
    g1 = world.gonic_playlist("Empty", [])
    _, gb = world.add_track("A/b.flac")
    g2 = world.gonic_playlist("One", [gb])
    plan = plan_merge(world.ctx, (ServerKind.GONIC, g1), (ServerKind.GONIC, g2), ServerKind.GONIC, "e")
    result = apply_merge(world.ctx, plan, secret=SECRET)
    assert result.status == "merged"
    assert world.gonic.entries(result.playlist_id) == [gb]

    # both empty -> gonic allows empty; plex does not
    plan2 = plan_merge(world.ctx, (ServerKind.GONIC, g1), (ServerKind.GONIC, g1), ServerKind.PLEX, "empty-plex")
    assert plan2.blocked


def test_smart_source_is_snapshot_readonly(world):
    a, _ = world.add_track("A/a.flac")
    smart = world.plex_playlist("Smart", [a], smart=True)
    g1 = world.gonic_playlist("G", [])
    before = list(world.plex_server._playlists[smart]._occurrences)
    plan = plan_merge(world.ctx, (ServerKind.PLEX, str(smart)), (ServerKind.GONIC, g1), ServerKind.GONIC, "from smart")
    assert any("smart" in n.lower() for n in plan.notes)
    apply_merge(world.ctx, plan, secret=SECRET)
    assert world.plex_server._playlists[smart]._occurrences == before


def test_unmatched_blocks_or_partial(world):
    a, _ = world.add_track("A/a.flac")
    lone, _ = world.add_track("A/lone.flac", plex_only=True)
    pa = world.plex_playlist("A", [a, lone])
    g1 = world.gonic_playlist("G", [])
    blocked = plan_merge(world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.GONIC, g1), ServerKind.GONIC, "m")
    assert blocked.blocked
    assert apply_merge(world.ctx, blocked, secret=SECRET).status == "blocked"

    partial = plan_merge(
        world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.GONIC, g1), ServerKind.GONIC, "m", allow_partial=True
    )
    assert not partial.blocked
    assert len(partial.desired_ids) == 1


def test_name_collision_requires_confirmation(world):
    a, _ = world.add_track("A/a.flac")
    pa = world.plex_playlist("A", [a])
    g1 = world.gonic_playlist("G", [])
    world.gonic_playlist("Merged", [])  # collision
    plan = plan_merge(world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.GONIC, g1), ServerKind.GONIC, "Merged")
    assert plan.collisions
    first = apply_merge(world.ctx, plan, secret=SECRET)
    assert first.status == "needs_confirmation"
    assert first.confirmation_token
    confirmed = apply_merge(world.ctx, plan, confirm_token=first.confirmation_token, secret=SECRET)
    assert confirmed.status == "merged"


def test_source_change_after_preview_is_stale(world):
    a, _ = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    pa = world.plex_playlist("A", [a])
    g1 = world.gonic_playlist("G", [])
    plan = plan_merge(world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.GONIC, g1), ServerKind.GONIC, "m")
    world.plex_server._occ_counter += 1
    world.plex_server._playlists[pa]._occurrences.append((world.plex_server._occ_counter, b))
    assert apply_merge(world.ctx, plan, secret=SECRET).status == "stale"


def test_duplicate_submission_is_deduped(world):
    a, _ = world.add_track("A/a.flac")
    pa = world.plex_playlist("A", [a])
    g1 = world.gonic_playlist("G", [])
    plan = plan_merge(world.ctx, (ServerKind.PLEX, str(pa)), (ServerKind.GONIC, g1), ServerKind.GONIC, "dedupe")
    first = apply_merge(world.ctx, plan, secret=SECRET)
    assert first.status == "merged"
    count = len(world.gonic.playlists)
    second = apply_merge(world.ctx, plan, secret=SECRET)
    assert second.status == "already_merged"
    assert len(world.gonic.playlists) == count
