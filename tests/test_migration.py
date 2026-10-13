from playlist_sync.engine.migration import apply_migration, confirmation_reasons, plan_migration
from playlist_sync.models import ServerKind

SECRET = "test-confirm-secret"


def test_plex_to_gonic_copy_preserves_order_and_duplicates(world):
    a, _ga = world.add_track("A/a.flac")
    b, _gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Roadtrip", [a, b, a])
    source_before = world.plex.get_playlist(str(pid)).track_ids()

    plan = plan_migration(world.ctx, ServerKind.PLEX, str(pid), ServerKind.GONIC, "Roadtrip (gonic)")
    assert not plan.blocked
    assert len(plan.desired_ids) == 3
    result = apply_migration(world.ctx, plan, enable_ongoing=False, secret=SECRET)
    assert result.status == "copied"
    assert world.gonic.entries(result.playlist_id) == plan.desired_ids
    # source untouched
    assert world.plex.get_playlist(str(pid)).track_ids() == source_before
    # one-time copy creates no scheduled mapping
    assert world.store.list_mappings() == []


def test_gonic_to_plex_copy_verified(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    gid = world.gonic_playlist("Mix", [gb, ga, gb])

    plan = plan_migration(world.ctx, ServerKind.GONIC, gid, ServerKind.PLEX, "Mix (plex)")
    result = apply_migration(world.ctx, plan, enable_ongoing=False, secret=SECRET)
    assert result.status == "copied"
    assert world.plex.get_playlist(result.playlist_id).track_ids() == [str(b), str(a), str(b)]


def test_unmatched_blocks_by_default_and_partial_is_opt_in(world):
    a, _ga = world.add_track("A/a.flac")
    lone, _ = world.add_track("A/lone.flac", plex_only=True)
    pid = world.plex_playlist("Mix", [a, lone])

    blocked = plan_migration(world.ctx, ServerKind.PLEX, str(pid), ServerKind.GONIC, "Copy")
    assert blocked.blocked
    result = apply_migration(world.ctx, blocked, enable_ongoing=False, secret=SECRET)
    assert result.status == "blocked"
    assert world.store.list_mappings() == []

    partial = plan_migration(
        world.ctx, ServerKind.PLEX, str(pid), ServerKind.GONIC, "Copy", allow_partial=True
    )
    assert not partial.blocked
    assert len(partial.desired_ids) == 1


def test_smart_plex_source_is_read_only(world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Smart", [a], smart=True)
    before = list(world.plex_server._playlists[pid]._occurrences)
    plan = plan_migration(world.ctx, ServerKind.PLEX, str(pid), ServerKind.GONIC, "From smart")
    assert not plan.blocked
    assert any("smart" in n.lower() for n in plan.notes)
    apply_migration(world.ctx, plan, enable_ongoing=False, secret=SECRET)
    # smart playlist is never written to
    assert world.plex_server._playlists[pid]._occurrences == before


def test_collision_requires_confirmation_bound_to_plan(world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    world.gonic_playlist("Mix", [])  # name collision on target

    plan = plan_migration(world.ctx, ServerKind.PLEX, str(pid), ServerKind.GONIC, "Mix")
    assert confirmation_reasons(plan)
    result = apply_migration(world.ctx, plan, enable_ongoing=False, secret=SECRET)
    assert result.status == "needs_confirmation"
    token = result.confirmation_token
    confirmed = apply_migration(
        world.ctx, plan, enable_ongoing=False, confirm_token=token, secret=SECRET
    )
    assert confirmed.status == "copied"


def test_ongoing_mapping_enabled_only_after_verified_population(world):
    a, _ga = world.add_track("A/a.flac")
    b, _gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Sync Me", [a, b])

    plan = plan_migration(
        world.ctx, ServerKind.PLEX, str(pid), ServerKind.GONIC, "Sync Me", enable_ongoing=True
    )
    result = apply_migration(world.ctx, plan, enable_ongoing=True, secret=SECRET)
    assert result.status == "copied_mapping_enabled"
    mappings = world.store.list_mappings()
    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.enabled and not mapping.paused
    assert mapping.direction == "plex_to_gonic"
    assert mapping.gonic_playlist_id == result.playlist_id
    assert mapping.baseline_ready is True


def test_zero_matched_to_plex_is_blocked(world):
    # gonic-only track cannot match any Plex track
    _, ga = world.add_track("A/g.flac", gonic_only=True)
    gid = world.gonic_playlist("G only", [ga])
    plan = plan_migration(world.ctx, ServerKind.GONIC, gid, ServerKind.PLEX, "copy")
    assert plan.blocked
    assert any("empty" in r.lower() or "matched" in r.lower() for r in plan.blocked_reasons)


def test_source_changed_after_preview_is_stale(world):
    a, _ga = world.add_track("A/a.flac")
    b, _gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a])
    plan = plan_migration(world.ctx, ServerKind.PLEX, str(pid), ServerKind.GONIC, "Mix")
    # external edit after preview
    world.plex_server._occ_counter += 1
    world.plex_server._playlists[pid]._occurrences.append((world.plex_server._occ_counter, b))
    result = apply_migration(world.ctx, plan, enable_ongoing=False, secret=SECRET)
    assert result.status == "stale"
    assert world.store.list_mappings() == []


def test_duplicate_apply_is_deduped(world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    plan = plan_migration(world.ctx, ServerKind.PLEX, str(pid), ServerKind.GONIC, "Mix")
    first = apply_migration(world.ctx, plan, enable_ongoing=False, secret=SECRET)
    assert first.status == "copied"
    count = len(world.gonic.playlists)
    second = apply_migration(world.ctx, plan, enable_ongoing=False, secret=SECRET)
    assert second.status == "already_copied"
    assert len(world.gonic.playlists) == count
