import pytest

from playlist_sync.backends import (
    BackendAuthError,
    BackendCapabilityError,
    GonicClient,
    normalize_mbid,
)


def test_gonic_bad_token_rejected(world):
    client = GonicClient(
        "http://gonic.test",
        "admin",
        "wrong-password",
        transport=world.gonic.transport(),
    )
    with pytest.raises(BackendAuthError):
        client.list_playlists()


def test_normalize_mbid_extracts_uuid():
    uuid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    assert normalize_mbid(f"mbid://recording/{uuid}") == uuid
    assert normalize_mbid(uuid.upper()) == uuid


def test_gonic_reads_order_and_duplicates(world):
    _, a = world.add_track("A/a.flac")
    _, b = world.add_track("A/b.flac")
    pid = world.gonic_playlist("Mix", [a, b, a, b])
    playlist = world.gonic_client.get_playlist(pid)
    assert playlist.track_ids() == [a, b, a, b]


def test_gonic_replace_preserves_order_and_duplicates(world):
    _, a = world.add_track("A/a.flac")
    _, b = world.add_track("A/b.flac")
    pid = world.gonic_playlist("Mix", [a, b])
    world.gonic_client.replace_playlist_entries(pid, [b, b, a])
    assert world.gonic.entries(pid) == [b, b, a]


def test_gonic_can_create_empty(world):
    pid = world.gonic_client.create_playlist("Empty", [])
    assert world.gonic.entries(pid) == []


def test_gonic_non_owner_is_readonly(world):
    _, a = world.add_track("A/a.flac")
    pid = world.gonic_playlist("Shared", [a], owner="someone-else", public=True)
    summaries = {s.playlist_id: s for s in world.gonic_client.list_playlists()}
    assert summaries[pid].writable is False


def test_gonic_replace_detects_missing_target_id(world):
    _, a = world.add_track("A/a.flac")
    # Writing to a non-existent id creates a *new* playlist in Gonic; the engine
    # avoids calling this path and re-checks existence first. Confirm the fake
    # reproduces the recreate-under-new-id behaviour so the guard is meaningful.
    before = set(world.gonic.playlists)
    new_id = world.gonic_client.replace_playlist_entries("pl-does-not-exist", [a])
    after = set(world.gonic.playlists)
    assert new_id is None  # replace returns nothing
    assert after - before  # a new playlist appeared


def test_gonic_chunked_replace_preserves_order(world):
    ids = [world.add_track(f"A/{i}.flac")[1] for i in range(5)]
    pid = world.gonic_playlist("Mix", [])
    client = GonicClient(
        "http://gonic.test", "admin", "gonic-pass", transport=world.gonic.transport(), chunk_size=2
    )
    client.replace_playlist_entries(pid, [*ids, ids[0]])
    assert world.gonic.entries(pid) == [*ids, ids[0]]


def test_plex_get_playlist_falls_back_to_listing_when_direct_fetch_fails(world):
    a, _ = world.add_track("A/a.flac")
    pid = world.plex_playlist("Shared", [a])
    # Simulate a server/permission where /playlist/<id> is not fetchable but the
    # playlist is still listed.
    world.plex_server.playlist_fetch_fails = True
    playlist = world.plex.get_playlist(str(pid))
    assert playlist.track_ids() == [str(a)]
    assert world.plex.list_playlists()  # listing still works


def test_plex_reads_order_and_occurrence_ids(world):
    a, _ = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a, b, a])
    playlist = world.plex.get_playlist(str(pid))
    assert playlist.track_ids() == [str(a), str(b), str(a)]
    occurrence_ids = [e.occurrence_id for e in playlist.entries]
    assert len(set(occurrence_ids)) == 3  # each occurrence distinct


def test_plex_reconcile_preserves_order_and_duplicates(world):
    a, _ = world.add_track("A/a.flac")
    b, _ = world.add_track("A/b.flac")
    c, _ = world.add_track("A/c.flac")
    pid = world.plex_playlist("Mix", [a, b, c, a])
    world.plex.replace_playlist_entries(str(pid), [str(c), str(a), str(c), str(b)])
    assert world.plex.get_playlist(str(pid)).track_ids() == [str(c), str(a), str(c), str(b)]


def test_plex_reconcile_to_empty(world):
    a, _ = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a, a])
    world.plex.replace_playlist_entries(str(pid), [])
    assert world.plex.get_playlist(str(pid)).track_ids() == []


def test_plex_smart_playlist_write_blocked(world):
    a, _ = world.add_track("A/a.flac")
    pid = world.plex_playlist("Smart", [a], smart=True)
    with pytest.raises(BackendCapabilityError):
        world.plex.replace_playlist_entries(str(pid), [str(a)])
    with pytest.raises(BackendCapabilityError):
        world.plex.rename_playlist(str(pid), "new name")


def test_plex_empty_create_blocked(world):
    with pytest.raises(BackendCapabilityError):
        world.plex.create_playlist("Empty", [])


def test_plex_create_and_append_chunking(world):
    keys = [world.add_track(f"A/{i}.flac")[0] for i in range(5)]
    pid = world.plex.create_playlist("Big", [str(k) for k in keys])
    assert world.plex.get_playlist(str(pid)).track_ids() == [str(k) for k in keys]
