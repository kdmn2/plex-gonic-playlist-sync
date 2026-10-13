"""Opt-in integration tests against real servers.

These are skipped unless explicitly enabled AND pointed at dedicated *test*
playlists. They never run against ordinary user playlists by default, and the
write test requires a second opt-in flag.

Enable read-only checks:

    PLAYLIST_SYNC_IT=1 \
    PLAYLIST_SYNC_IT_PLEX_URL=http://plex:32400 \
    PLAYLIST_SYNC_IT_PLEX_TOKEN=... \
    PLAYLIST_SYNC_IT_GONIC_URL=http://gonic:4747 \
    PLAYLIST_SYNC_IT_GONIC_USER=admin \
    PLAYLIST_SYNC_IT_GONIC_PASSWORD=... \
    pytest -m integration

Enable the guarded write test (only touches the named test playlists):

    ... PLAYLIST_SYNC_IT_APPLY=1 \
        PLAYLIST_SYNC_IT_TEST_GONIC_PLAYLIST=<id-of-a-throwaway-playlist>
"""

from __future__ import annotations

import os

import pytest

from playlist_sync.backends import GonicClient, PlexClient
from playlist_sync.matching.paths import PathMapper, PathRoot

pytestmark = pytest.mark.integration

ENABLED = os.environ.get("PLAYLIST_SYNC_IT") == "1"

requires_it = pytest.mark.skipif(not ENABLED, reason="set PLAYLIST_SYNC_IT=1 to enable integration tests")


def _plex() -> PlexClient:
    roots = os.environ.get("PLAYLIST_SYNC_IT_PLEX_ROOT")
    gonic_roots = os.environ.get("PLAYLIST_SYNC_IT_GONIC_ROOT")
    mapper = (
        PathMapper([PathRoot("it", roots, gonic_roots)])
        if roots and gonic_roots
        else PathMapper([])
    )
    return PlexClient(
        os.environ["PLAYLIST_SYNC_IT_PLEX_URL"],
        os.environ["PLAYLIST_SYNC_IT_PLEX_TOKEN"],
        path_mapper=mapper,
    )


def _gonic() -> GonicClient:
    return GonicClient(
        os.environ["PLAYLIST_SYNC_IT_GONIC_URL"],
        os.environ["PLAYLIST_SYNC_IT_GONIC_USER"],
        os.environ["PLAYLIST_SYNC_IT_GONIC_PASSWORD"],
    )


@requires_it
def test_plex_lists_playlists_and_reads_one():
    plex = _plex()
    try:
        playlists = plex.list_playlists()
        assert playlists, "no Plex music playlists found"
        first = plex.get_playlist(playlists[0].playlist_id)
        assert first.track_ids() is not None
    finally:
        plex.close()


@requires_it
def test_gonic_lists_playlists_and_reads_one():
    gonic = _gonic()
    try:
        playlists = gonic.list_playlists()
        assert playlists, "no Gonic playlists found"
        first = gonic.get_playlist(playlists[0].playlist_id)
        # order + duplicates round-trip
        assert first.track_ids() == [e.track.track_id for e in first.entries]
    finally:
        gonic.close()


@requires_it
@pytest.mark.skipif(
    os.environ.get("PLAYLIST_SYNC_IT_APPLY") != "1",
    reason="set PLAYLIST_SYNC_IT_APPLY=1 to enable the guarded write test",
)
def test_gonic_replace_round_trip_on_designated_playlist():
    playlist_id = os.environ["PLAYLIST_SYNC_IT_TEST_GONIC_PLAYLIST"]
    gonic = _gonic()
    try:
        original = gonic.get_playlist(playlist_id)
        original_ids = original.track_ids()
        try:
            # reverse + duplicate the current contents, then restore
            gonic.replace_playlist_entries(playlist_id, list(reversed(original_ids)) + original_ids[:1])
            mutated = gonic.get_playlist(playlist_id)
            assert mutated.track_ids() == list(reversed(original_ids)) + original_ids[:1]
        finally:
            gonic.replace_playlist_entries(playlist_id, original_ids)
        restored = gonic.get_playlist(playlist_id)
        assert restored.track_ids() == original_ids
    finally:
        gonic.close()
