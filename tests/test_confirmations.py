import httpx
import pytest

from playlist_sync.backends import GonicClient
from playlist_sync.backends import gonic as gonic_module
from playlist_sync.engine.sync import sync_mapping
from playlist_sync.errors import AppError
from playlist_sync.models import Direction


def make_mapping(world, *, direction, plex_id=None, gonic_id=None, **kw):
    return world.store.create_mapping(
        name="m",
        direction=direction.value,
        plex_playlist_id=str(plex_id) if plex_id is not None else None,
        gonic_playlist_id=gonic_id,
        **kw,
    )


def test_pagination_loop_is_detected(monkeypatch):
    """A server that repeats the same page must not be treated as complete."""
    monkeypatch.setattr(gonic_module, "PAGE_SIZE", 1)

    def handler(request):
        path = request.url.path
        if path.endswith("getAlbumList2"):
            return httpx.Response(
                200,
                json={
                    "subsonic-response": {
                        "status": "ok",
                        "albumList2": {"album": [{"id": "al-1"}]},
                    }
                },
            )
        return httpx.Response(200, json={"subsonic-response": {"status": "ok", "album": {"song": []}}})

    client = GonicClient(
        "http://gonic.test",
        "admin",
        "secret",
        transport=httpx.MockTransport(handler),
        max_page_repeats=2,
    )
    with pytest.raises(AppError) as excinfo:
        list(client.iter_library_tracks())
    assert excinfo.value.code == "PAGINATION_LOOP"


def test_destructive_change_requires_confirmation(world):
    _a, ga = world.add_track("A/a.flac")
    world.settings.sync.allow_clear_target_when_source_empty = True
    pid = world.plex_playlist("Mix", [])  # empty source
    gid = world.gonic_playlist("Mix", [ga])  # populated target
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)

    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status == "confirmation_required"
    assert "clear" in result.detail.lower()
    assert world.gonic.entries(gid) == [ga]  # not mutated without confirmation

    token = result.extra["confirmation_token"]
    confirmed = sync_mapping(world.ctx, mapping.id, dry_run=False, confirm_token=token)
    assert confirmed.status == "applied_verified"
    assert world.gonic.entries(gid) == []


def test_stale_confirmation_is_rejected(world):
    _a, ga = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    world.settings.sync.allow_clear_target_when_source_empty = True
    pid = world.plex_playlist("Mix", [])  # empty source -> clearing the target needs confirmation
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)

    first = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert first.status == "confirmation_required"
    token = first.extra["confirmation_token"]

    # Someone edits the target after the preview -> the plan fingerprint changes.
    world.gonic.playlists[gid].entries.append(gb)

    second = sync_mapping(world.ctx, mapping.id, dry_run=False, confirm_token=token)
    assert second.status == "confirmation_required"
    assert second.extra.get("stale") is True
    assert world.gonic.entries(gid) == [ga, gb]  # not mutated


def test_force_bypasses_confirmation(world):
    _a, ga = world.add_track("A/a.flac")
    world.settings.sync.allow_clear_target_when_source_empty = True
    pid = world.plex_playlist("Mix", [])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    result = sync_mapping(world.ctx, mapping.id, dry_run=False, force=True)
    assert result.status == "applied_verified"
    assert world.gonic.entries(gid) == []
