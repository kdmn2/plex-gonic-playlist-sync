"""Engine context: settings + lazily-built backend clients + state store.

Backend clients are constructed on first use so that the service starts (and
``doctor``/``mappings``/``snapshots`` run) even when Plex or Gonic is briefly
unreachable. This is what keeps a backend outage from causing a restart loop.
"""

from __future__ import annotations

import logging

from ..backends import GonicClient, PlexClient
from ..config import AppSettings, build_path_mapper
from ..matching.index import TargetIndex
from ..matching.paths import PathMapper
from ..models import ServerKind
from ..store import Store

log = logging.getLogger("playlist_sync.engine")


def build_plex(settings: AppSettings, mapper: PathMapper) -> PlexClient:
    return PlexClient(
        settings.plex.url,
        settings.plex_secret(),
        path_mapper=mapper,
        library_sections=settings.plex.library_sections,
        timeout=settings.plex.timeout,
        verify=settings.verify_for("plex"),
    )


def build_gonic(settings: AppSettings, mapper: PathMapper) -> GonicClient:
    return GonicClient(
        settings.gonic.url,
        settings.gonic.username,
        settings.gonic_secret(),
        path_mapper=mapper,
        music_folder_id=settings.gonic.music_folder_id,
        timeout=settings.gonic.timeout,
        verify=settings.verify_for("gonic"),
        use_token_auth=settings.gonic.use_token_auth,
        chunk_size=settings.sync.append_chunk_size,
    )


def build_clients(settings: AppSettings) -> tuple[PlexClient, GonicClient, PathMapper]:
    mapper = build_path_mapper(settings)
    return build_plex(settings, mapper), build_gonic(settings, mapper), mapper


class EngineContext:
    def __init__(
        self,
        settings: AppSettings,
        store: Store,
        path_mapper: PathMapper | None = None,
        plex: PlexClient | None = None,
        gonic: GonicClient | None = None,
    ):
        self.settings = settings
        self.store = store
        self.path_mapper = path_mapper or build_path_mapper(settings)
        self._plex = plex
        self._gonic = gonic
        self.index_cache = {}

    @classmethod
    def create(cls, settings: AppSettings, *, store: Store | None = None) -> EngineContext:
        if store is None:
            store = Store(settings.data_dir / "state.sqlite3")
        return cls(settings=settings, store=store)

    @property
    def plex(self) -> PlexClient:
        if self._plex is None:
            self._plex = build_plex(self.settings, self.path_mapper)
        return self._plex

    @property
    def gonic(self) -> GonicClient:
        if self._gonic is None:
            self._gonic = build_gonic(self.settings, self.path_mapper)
        return self._gonic

    def backend(self, side: ServerKind):
        return self.plex if side == ServerKind.PLEX else self.gonic

    def library_index(self, side: ServerKind, *, refresh: bool = False) -> TargetIndex:
        key = side.value
        if refresh or key not in self.index_cache:
            backend = self.backend(side)
            index = TargetIndex()
            index.source = key
            index.extend(backend.iter_library_tracks())
            self.index_cache[key] = index
        return self.index_cache[key]

    def invalidate_index(self, side: ServerKind | None = None) -> None:
        if side is None:
            self.index_cache.clear()
        else:
            self.index_cache.pop(side.value, None)

    def server_identity(self, side: ServerKind) -> str:
        if side == ServerKind.PLEX:
            return str(getattr(self.plex._server, "machineIdentifier", "") or self.settings.plex.url)
        return self.settings.gonic.url

    def friendly_name(self, side: ServerKind) -> str:
        if side == ServerKind.PLEX:
            return str(getattr(self.plex._server, "friendlyName", "") or "plex")
        return "gonic"

    def register_servers(self) -> None:
        for side in (ServerKind.PLEX, ServerKind.GONIC):
            self.store.upsert_server(
                side.value,
                self.server_identity(side),
                self.friendly_name(side),
                self.settings.plex.url if side == ServerKind.PLEX else self.settings.gonic.url,
            )

    def close(self) -> None:
        import contextlib

        for client in (self._plex, self._gonic):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
