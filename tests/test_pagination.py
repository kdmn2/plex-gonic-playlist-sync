from playlist_sync.backends import gonic as gonic_module


def test_gonic_library_pagination(world, monkeypatch):
    monkeypatch.setattr(gonic_module, "PAGE_SIZE", 2)
    # create 5 albums with 1 track each
    for i in range(5):
        world.add_track(f"Disc{i}/t.flac", title=f"T{i}")
    tracks = list(world.gonic_client.iter_library_tracks())
    assert len(tracks) == 5


def test_large_gonic_playlist_order_preserved(world):
    ids = []
    for i in range(50):
        _, gid = world.add_track(f"A/{i:03d}.flac")
        ids.append(gid)
    pid = world.gonic_playlist("Big", ids + ids)
    playlist = world.gonic_client.get_playlist(pid)
    assert playlist.track_ids() == ids + ids
