from playlist_sync.engine.sync import sync_mapping
from playlist_sync.models import Direction


def make_bidi(world, *, plex_id, gonic_id, baseline_mode):
    return world.store.create_mapping(
        name="bidi",
        direction=Direction.BIDIRECTIONAL.value,
        plex_playlist_id=str(plex_id),
        gonic_playlist_id=gonic_id,
        baseline_mode=baseline_mode,
    )


def append_plex(world, pid, key):
    world.plex_server._occ_counter += 1
    world.plex_server._playlists[pid]._occurrences.append((world.plex_server._occ_counter, key))


def append_gonic(world, gid, track_id):
    world.gonic.playlists[gid].entries.append(track_id)


def test_bootstrap_manual_review_blocks(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_bidi(world, plex_id=pid, gonic_id=gid, baseline_mode="manual_review")
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "blocked"
    assert "baseline" in result.detail.lower() or "review" in result.detail.lower()


def test_bootstrap_plex_wins_then_propagates_changes(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_bidi(world, plex_id=pid, gonic_id=gid, baseline_mode="plex_wins")

    assert sync_mapping(world.ctx, mapping.id, dry_run=False).status == "applied_verified"
    assert world.gonic.entries(gid) == [ga]
    assert world.store.get_mapping(mapping.id).baseline_ready is True

    # now only Plex changes
    append_plex(world, pid, b)
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "applied_verified"
    assert world.gonic.entries(gid) == [ga, gb]


def test_only_gonic_changed_propagates_to_plex(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_bidi(world, plex_id=pid, gonic_id=gid, baseline_mode="plex_wins")
    assert sync_mapping(world.ctx, mapping.id, dry_run=False).status == "no_change"

    append_gonic(world, gid, gb)
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "applied_verified"
    assert world.plex.get_playlist(str(pid)).track_ids() == [str(a), str(b)]


def test_both_changed_identically_refreshes_baseline(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_bidi(world, plex_id=pid, gonic_id=gid, baseline_mode="plex_wins")
    assert sync_mapping(world.ctx, mapping.id, dry_run=False).status == "no_change"

    append_plex(world, pid, b)
    append_gonic(world, gid, gb)
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "no_change"
    assert "identical" in result.detail.lower()
    assert world.gonic.entries(gid) == [ga, gb]


def test_both_changed_differently_is_a_conflict(world):
    a, ga = world.add_track("A/a.flac")
    b, _gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_bidi(world, plex_id=pid, gonic_id=gid, baseline_mode="plex_wins")
    assert sync_mapping(world.ctx, mapping.id, dry_run=False).status == "no_change"

    # Plex adds b; Gonic drops a — genuinely divergent
    append_plex(world, pid, b)
    world.gonic.playlists[gid].entries = []
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "conflict"
    # neither side mutated
    assert world.plex.get_playlist(str(pid)).track_ids() == [str(a), str(b)]
    assert world.gonic.entries(gid) == []


def test_smart_plex_blocks_bidirectional(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Smart", [a], smart=True)
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_bidi(world, plex_id=pid, gonic_id=gid, baseline_mode="plex_wins")
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "blocked"
    assert any("smart" in r.lower() for r in (result.plan.blocked_reasons if result.plan else []))
