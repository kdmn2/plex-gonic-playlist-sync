"""Shared test fixtures and a small world builder."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from fakes import (
    FakeGonic,
    FakePlexPlaylist,
    FakePlexServer,
    FakePlexTrack,
    FakeSong,
)
from playlist_sync.backends import GonicClient, PlexClient
from playlist_sync.config import (
    AppSettings,
    GonicSettings,
    PathRootSettings,
    PlexSettings,
    SyncSettings,
)
from playlist_sync.engine.context import EngineContext
from playlist_sync.matching.paths import PathMapper, PathRoot
from playlist_sync.store import Store


def make_settings(tmp_path: Path, *, path_roots=None, **sync_overrides) -> AppSettings:
    roots = path_roots if path_roots is not None else [("/plex-music", "/gonic-music")]
    sync = SyncSettings(
        path_roots=[PathRootSettings(name=f"r{i}", plex_root=p, gonic_root=g) for i, (p, g) in enumerate(roots)],
        **sync_overrides,
    )
    return AppSettings(
        data_dir=tmp_path,
        config_dir=tmp_path,
        plex=PlexSettings(url="http://plex.test", token="plex-token"),
        gonic=GonicSettings(url="http://gonic.test", username="admin", password="gonic-pass"),
        sync=sync,
    )


class World:
    """A matched Plex + Gonic pair sharing one relative path space."""

    def __init__(self, tmp_path: Path, *, path_roots=None, **sync_overrides):
        self.tmp_path = tmp_path
        self.settings = make_settings(tmp_path, path_roots=path_roots, **sync_overrides)
        self.path_mapper = PathMapper(
            [PathRoot(name=r.name, plex_root=r.plex_root, gonic_root=r.gonic_root) for r in self.settings.sync.path_roots]
        )
        self.gonic = FakeGonic(username=self.settings.gonic.username, password="gonic-pass")
        self.plex_tracks: list[FakePlexTrack] = []
        self._plex_key = itertools.count(1)
        self._gonic_key = itertools.count(1)
        self.plex_server = FakePlexServer(self.plex_tracks)
        self._plex_counter = itertools.count(1)
        self.plex = PlexClient(
            "http://plex.test",
            "plex-token",
            path_mapper=self.path_mapper,
            server_factory=lambda *a, **k: self.plex_server,
        )
        self.gonic_client = GonicClient(
            "http://gonic.test",
            self.settings.gonic.username,
            "gonic-pass",
            path_mapper=self.path_mapper,
            transport=self.gonic.transport(),
        )
        self.store = Store(tmp_path / "state.sqlite3")
        self.ctx = EngineContext(
            settings=self.settings,
            store=self.store,
            plex=self.plex,
            gonic=self.gonic_client,
            path_mapper=self.path_mapper,
        )

    # -- building --------------------------------------------------------------

    def add_track(
        self,
        rel_path: str,
        *,
        title: str | None = None,
        artist: str = "Artist",
        album: str = "Album",
        disc: int = 1,
        number: int = 1,
        duration_ms: int = 200_000,
        mbid: str = "",
        isrc: str = "",
        gonic_title: str | None = None,
        plex_only: bool = False,
        gonic_only: bool = False,
    ) -> tuple[int | None, str | None]:
        """Create matching tracks on both sides; returns (plex_key, gonic_id)."""
        title = title or rel_path.rsplit("/", 1)[-1]
        plex_key = None
        gonic_id = None
        if not gonic_only:
            plex_key = next(self._plex_counter)
            track = FakePlexTrack(
                ratingKey=plex_key,
                title=title,
                grandparentTitle=artist,
                originalTitle=artist,
                parentTitle=album,
                parentIndex=disc,
                index=number,
                duration=duration_ms,
                locations=[f"/plex-music/{rel_path}"],
                guid_ids=[f"mbid://recording/{mbid}"] if mbid else [],
            )
            self.plex_tracks.append(track)
            self.plex_server.tracks[plex_key] = track
        if not plex_only:
            gonic_id = f"tr-{next(self._gonic_key)}"
            self.gonic.add_song(
                FakeSong(
                    id=gonic_id,
                    title=gonic_title or title,
                    artist=artist,
                    album=album,
                    album_id=f"al-{disc}",
                    track=number,
                    disc=disc,
                    duration=duration_ms // 1000,
                    path=rel_path,
                    musicbrainz_id=mbid,
                    isrc=[isrc] if isrc else [],
                    album_artist=artist,
                )
            )
        return plex_key, gonic_id

    def plex_playlist(self, name: str, keys: list[int], *, smart: bool = False) -> int:
        rating = next(self._plex_counter)
        playlist = FakePlexPlaylist(ratingKey=rating, title=name, smart=smart, server=self.plex_server)
        for key in keys:
            self.plex_server._occ_counter += 1
            playlist._occurrences.append((self.plex_server._occ_counter, key))
        self.plex_server._playlists[rating] = playlist
        return rating

    def gonic_playlist(self, name: str, ids: list[str], *, owner: str | None = None, public: bool = False) -> str:
        return self.gonic.add_playlist(name, ids, owner=owner, public=public)

    def close(self) -> None:
        self.ctx.close()
        self.store.close()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()
