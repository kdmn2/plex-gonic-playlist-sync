"""Engine context: settings + lazily-built backend clients + state store.

Backend clients are constructed on first use so that the service starts (and
``doctor``/``mappings``/``snapshots`` run) even when Plex or Gonic is briefly
unreachable. The state store is opened defensively: if it cannot be opened or is
integrity-compromised, the context records a typed ``state_error`` and disables
mutations while keeping diagnostics available.
"""

from __future__ import annotations

import logging
import threading
import time

from ..backends import GonicClient, PlexClient
from ..config import AppSettings, build_path_mapper
from ..errors import AppError, make_error
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
        retry_attempts=settings.resilience.retry_attempts,
        retry_base_seconds=settings.resilience.retry_base_seconds,
        retry_max_seconds=settings.resilience.retry_max_seconds,
        circuit_failure_threshold=settings.resilience.circuit_failure_threshold,
        circuit_reset_seconds=settings.resilience.circuit_reset_seconds,
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
        max_response_bytes=settings.limits.max_response_bytes,
        retry_attempts=settings.resilience.retry_attempts,
        retry_base_seconds=settings.resilience.retry_base_seconds,
        retry_max_seconds=settings.resilience.retry_max_seconds,
        retry_total_seconds=settings.resilience.retry_total_seconds,
        circuit_failure_threshold=settings.resilience.circuit_failure_threshold,
        circuit_reset_seconds=settings.resilience.circuit_reset_seconds,
    )


def build_clients(settings: AppSettings) -> tuple[PlexClient, GonicClient, PathMapper]:
    mapper = build_path_mapper(settings)
    return build_plex(settings, mapper), build_gonic(settings, mapper), mapper


class EngineContext:
    def __init__(
        self,
        settings: AppSettings,
        store: Store | None = None,
        path_mapper: PathMapper | None = None,
        plex: PlexClient | None = None,
        gonic: GonicClient | None = None,
        *,
        state_error: AppError | None = None,
    ):
        self.settings = settings
        self._store = store
        self.path_mapper = path_mapper or build_path_mapper(settings)
        self._plex = plex
        self._gonic = gonic
        self.index_cache: dict[str, TargetIndex] = {}
        # Per-side build status for the background warm-up and UI indicator.
        self.index_status: dict[str, dict] = {
            ServerKind.PLEX.value: {"state": "idle", "tracks": 0},
            ServerKind.GONIC.value: {"state": "idle", "tracks": 0},
        }
        self._index_locks = {
            ServerKind.PLEX.value: threading.Lock(),
            ServerKind.GONIC.value: threading.Lock(),
        }
        from .runner import BackgroundRunner

        self.runner = BackgroundRunner(self)
        # Prefer a store-reported integrity error, otherwise any explicit error.
        self.state_error = state_error or (store.state_error if store is not None else None)

    @classmethod
    def create(cls, settings: AppSettings, *, store: Store | None = None) -> EngineContext:
        if store is None:
            try:
                store = Store(
                    settings.data_dir / "state.sqlite3",
                    identity_file=settings.config_dir / "installation.json",
                )
            except AppError as exc:
                log.error("state store unavailable: %s (%s)", exc, exc.code)
                return cls(settings=settings, store=None, state_error=exc)
        return cls(settings=settings, store=store)

    @property
    def store(self) -> Store:
        if self._store is None:
            raise self.state_error or make_error(
                "STORAGE_UNAVAILABLE", "The state store is not available."
            )
        return self._store

    @property
    def has_store(self) -> bool:
        return self._store is not None

    @property
    def storage_ready(self) -> bool:
        return self._store is not None and self.state_error is None

    def ensure_mutable(self) -> None:
        if self.state_error is not None:
            raise self.state_error
        store = self._store
        if store is None:
            raise make_error("STORAGE_UNAVAILABLE", "The state store is not available.")
        store.require_mutable()

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
        if refresh:
            self.index_cache.pop(key, None)
        if key in self.index_cache:
            return self.index_cache[key]
        return self.build_index(side)

    def build_index(self, side: ServerKind) -> TargetIndex:
        """Build (and cache) one library index, updating progress status.

        Guarded by a per-side lock so a background warm-up and an interactive
        request cannot build the same index twice concurrently.
        """
        key = side.value
        with self._index_locks[key]:
            if key in self.index_cache:
                return self.index_cache[key]
            from ..store.db import utcnow

            status = self.index_status.setdefault(key, {"state": "idle", "tracks": 0})
            status.update(
                state="building", started_at=utcnow(), updated_at=utcnow(), finished_at=None, error=None, tracks=0
            )
            started = time.perf_counter()
            index = TargetIndex()
            index.source = key
            count = 0
            log.info("building %s library index...", key)
            try:
                for track in self.backend(side).iter_library_tracks():
                    index.add(track)
                    count += 1
                    if count % 500 == 0:
                        status.update(tracks=count, updated_at=utcnow())
            except Exception as exc:
                status.update(
                    state="error",
                    error=getattr(exc, "code", type(exc).__name__),
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
                log.warning("%s library index build failed: %s", key, exc)
                raise
            self.index_cache[key] = index
            status.update(
                state="ready",
                tracks=index.size,
                duration=round(time.perf_counter() - started, 1),
                finished_at=utcnow(),
                updated_at=utcnow(),
            )
            log.info(
                "%s library index built: %d tracks in %.1fs",
                key,
                index.size,
                time.perf_counter() - started,
            )
            return index

    def index_status_snapshot(self) -> dict:
        snapshot: dict = {}
        for key, status in self.index_status.items():
            entry = dict(status)
            entry["cached"] = key in self.index_cache
            snapshot[key] = entry
        return snapshot

    def invalidate_index(self, side: ServerKind | None = None) -> None:
        if side is None:
            self.index_cache.clear()
            for status in self.index_status.values():
                status.update(state="idle", tracks=0, finished_at=None)
        else:
            self.index_cache.pop(side.value, None)
            self.index_status.setdefault(side.value, {}).update(state="idle", tracks=0)

    def reset_clients(self) -> None:
        """Drop cached backend clients so credentials/URLs are re-read on next use."""
        import contextlib

        for client in (self._plex, self._gonic):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
        self._plex = None
        self._gonic = None
        self.index_cache.clear()

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

    def storage_health(self) -> dict:
        if self._store is None:
            return {
                "available": False,
                "state_error": self.state_error.to_dict(include_details=True) if self.state_error else None,
            }
        health = self._store.storage_health(min_free_bytes=self.settings.limits.min_free_bytes)
        health["available"] = True
        return health

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            if self._store is not None:
                self._store.close()
        for client in (self._plex, self._gonic):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
