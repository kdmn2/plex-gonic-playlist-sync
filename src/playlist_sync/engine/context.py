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
from ..store.db import utcnow
from . import index_cache

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
        # Per-side generation metadata, last content-check time, and counters for
        # diagnostics. The index is disposable; authoritative state lives in store.
        self._index_generation: dict[str, dict] = {}
        self._index_validated_at: dict[str, float] = {}
        self._index_counters: dict[str, dict] = {}
        self._index_epoch: dict[str, int] = {}
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
        # Timestamp this process started: used to tell a live operation *owned by
        # this process* from an orphan left behind by a previous one (e.g. after
        # a container restart), so orphans are reconciled instead of skipped.
        self.process_started_at = utcnow()
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
        """Non-validating accessor: return the cached index or build one.

        Reconciliation/verification paths use this to reuse the generation the
        plan was made against. Use :meth:`ensure_library_index` before planning.
        """
        key = side.value
        if refresh:
            self.index_cache.pop(key, None)
        if key in self.index_cache:
            return self.index_cache[key]
        return self.build_index(side)

    # -- change-aware, persisted library index --------------------------------

    def _index_meta(self, side: ServerKind) -> dict:
        return {
            "algorithm_version": index_cache.INDEX_ALGORITHM_VERSION,
            "config_revision": index_cache.matching_config_revision(self.settings),
            "library_scope": index_cache.library_scope(self.settings, side),
        }

    def _load_persisted_generation(self, side: ServerKind) -> bool:
        """Adopt a compatible *completed* persisted generation (no rebuild).

        Returns True when a usable generation was loaded. The generation is
        marked *awaiting validation*: it is never treated as current merely
        because it exists.
        """
        key = side.value
        if not self.has_store:
            return False
        meta = self._index_meta(side)
        try:
            row = self.store.load_library_index(
                server_kind=side.value,
                server_identity=self.server_identity(side),
                library_scope=meta["library_scope"],
                algorithm_version=meta["algorithm_version"],
                config_revision=meta["config_revision"],
            )
        except Exception as exc:
            log.warning("%s index load failed: %s", key, exc)
            return False
        if not row or not row.get("complete") or not row.get("payload"):
            return False
        try:
            tracks = index_cache.deserialize_tracks(row["payload"])
        except Exception as exc:
            log.warning("%s persisted index unreadable; ignoring (cache only): %s", key, exc)
            return False
        index = TargetIndex()
        index.source = key
        index.extend(tracks)
        self.index_cache[key] = index
        self._index_generation[key] = {
            "source": "persisted",
            "fingerprint": row.get("fingerprint"),
            "change_token": row.get("change_token"),
            "validated": False,
            "track_count": row.get("track_count"),
            "built_at": row.get("built_at"),
            "persisted_validated_at": row.get("validated_at"),
        }
        return True

    def _persist_generation(self, side: ServerKind, tracks, token, fingerprint: str) -> None:
        if not self.has_store:
            return
        meta = self._index_meta(side)
        try:
            self.store.save_library_index(
                server_kind=side.value,
                server_identity=self.server_identity(side),
                library_scope=meta["library_scope"],
                algorithm_version=meta["algorithm_version"],
                config_revision=meta["config_revision"],
                change_token=token,
                fingerprint=fingerprint,
                track_count=len(tracks),
                payload=index_cache.serialize_tracks(tracks),
            )
        except Exception as exc:  # cache is disposable; never fail an operation on it
            log.warning("%s index persist failed (cache only): %s", side.value, exc)

    def _mark_validated(self, side: ServerKind, token: str | None) -> None:
        key = side.value
        self._index_validated_at[key] = time.monotonic()
        gen = self._index_generation.get(key)
        if gen is not None:
            gen["validated"] = True
            if token is not None:
                gen["change_token"] = token
        if self.has_store:
            import contextlib

            meta = self._index_meta(side)
            with contextlib.suppress(Exception):
                self.store.mark_library_index_validated(
                    server_kind=side.value,
                    server_identity=self.server_identity(side),
                    library_scope=meta["library_scope"],
                    algorithm_version=meta["algorithm_version"],
                    config_revision=meta["config_revision"],
                    change_token=token,
                )

    def _recently_validated(self, key: str) -> bool:
        window = float(getattr(self.settings.limits, "index_validate_seconds", 0) or 0)
        if window <= 0:
            return False
        last = self._index_validated_at.get(key)
        return last is not None and (time.monotonic() - last) < window

    def ensure_library_index(self, side: ServerKind, *, progress=None, force: bool = False) -> TargetIndex:
        """Validate freshness, then return a completed index generation.

        Order of operations (cheapest first):

        1. A trustworthy backend change token that matches the current
           generation lets us reuse it **without reading the library**.
        2. A persisted generation is adopted without rebuilding.
        3. Otherwise the current library is read and fingerprinted; a matching
           fingerprint reuses the existing parsed index (no rebuild, no write)
           -- this still *fetches*, it just avoids the expensive re-index.
        4. A different fingerprint publishes a new generation atomically.

        A failed/incomplete read never publishes a generation and never replaces
        the previous one; callers raise so mutating operations block.
        """
        key = side.value
        status = self.index_status.setdefault(key, {"state": "idle", "tracks": 0})
        counters = self._index_counters.setdefault(
            key, {"checks": 0, "skipped": 0, "reads": 0, "reused": 0, "rebuilds": 0, "errors": 0}
        )
        counters["checks"] += 1
        start_epoch = self._index_epoch.get(key, 0)
        with self._index_locks[key]:
            gen = self._index_generation.get(key)
            index = self.index_cache.get(key)

            # Coalesce concurrent refreshes: if another caller completed a
            # validation while we waited for the lock, adopt its result instead of
            # reading the same scope again. (Sequential callers are unaffected.)
            if not force and index is not None and self._index_epoch.get(key, 0) != start_epoch:
                counters["skipped"] += 1
                status.update(state="ready", stale=False, source="coalesced", tracks=index.size)
                return index

            token: str | None = None
            try:
                token = self.backend(side).library_change_token()
            except Exception:
                token = None

            # 1. in-memory generation proven current by a matching token
            if not force and index is not None and gen is not None and token is not None \
                    and gen.get("change_token") == token and gen.get("validated"):
                counters["skipped"] += 1
                self._index_validated_at[key] = time.monotonic()
                status.update(state="ready", stale=False, source="token", tracks=index.size, change_token=token)
                return index

            # 1b. no trustworthy token: reuse a recent successful content check
            if not force and index is not None and gen is not None and token is None \
                    and self._recently_validated(key):
                counters["skipped"] += 1
                status.update(state="ready", stale=False, source="reused", tracks=index.size)
                return index

            # 2. adopt a compatible persisted generation (no rebuild)
            if index is None and self._load_persisted_generation(side):
                index = self.index_cache.get(key)
                gen = self._index_generation.get(key)
                status.update(source="persisted")

            # 2b. persisted generation already matches the token: skip the read
            if not force and index is not None and gen is not None and token is not None \
                    and gen.get("change_token") == token:
                counters["skipped"] += 1
                self._mark_validated(side, token)
                status.update(state="ready", stale=False, source="persisted", tracks=index.size,
                              change_token=token)
                return index

            # 3/4. establish freshness by reading the current library.
            from ..store.db import utcnow

            status.update(state="checking", updated_at=utcnow(), stale=False, error=None)
            if progress is not None:
                progress.stage(
                    "checking_index", f"Checking the {key} library for changes",
                    completed=0, total=None, unit="tracks",
                )
            started = time.perf_counter()
            try:
                tracks = list(self.backend(side).iter_library_tracks())
            except Exception as exc:
                counters["errors"] += 1
                code = getattr(exc, "code", type(exc).__name__)
                log.warning("%s library freshness check failed: %s", key, exc)
                if index is not None:
                    # keep the last completed generation but label it stale
                    status.update(state="ready", stale=True, error=code, updated_at=utcnow())
                else:
                    status.update(state="error", error=code, finished_at=utcnow(), updated_at=utcnow())
                raise
            counters["reads"] += 1
            fingerprint = index_cache.fingerprint_tracks(tracks)

            if index is not None and gen is not None and gen.get("fingerprint") == fingerprint:
                counters["reused"] += 1
                self._index_epoch[key] = self._index_epoch.get(key, 0) + 1
                self._mark_validated(side, token)
                status.update(
                    state="ready", stale=False, source="content-unchanged", tracks=index.size,
                    fingerprint=fingerprint, change_token=token, duration=round(time.perf_counter() - started, 1),
                    updated_at=utcnow(), finished_at=utcnow(),
                )
                if progress is not None:
                    progress.progress(
                        completed=len(tracks), total=len(tracks), unit="tracks",
                        counters={"checked": len(tracks), "rebuilt": 0},
                        desc="No changes detected; using the existing index", force=True,
                    )
                return index

            # publish atomically: build fully, swap memory, then persist
            counters["rebuilds"] += 1
            new_index = TargetIndex()
            new_index.source = key
            new_index.extend(tracks)
            self.index_cache[key] = new_index
            self._index_generation[key] = {
                "source": "rebuilt",
                "fingerprint": fingerprint,
                "change_token": token,
                "validated": True,
                "track_count": new_index.size,
                "built_at": utcnow(),
            }
            self._index_validated_at[key] = time.monotonic()
            self._persist_generation(side, tracks, token, fingerprint)
            self._index_epoch[key] = self._index_epoch.get(key, 0) + 1
            status.update(
                state="ready", stale=False, source="rebuilt", tracks=new_index.size,
                fingerprint=fingerprint, change_token=token,
                duration=round(time.perf_counter() - started, 1), updated_at=utcnow(), finished_at=utcnow(),
            )
            if progress is not None:
                progress.progress(
                    completed=new_index.size, total=new_index.size, unit="tracks",
                    counters={"checked": len(tracks), "rebuilt": 1},
                    desc=f"Library changed; index rebuilt ({new_index.size} tracks)", force=True,
                )
            log.info("%s library index rebuilt: %d tracks", key, new_index.size)
            return new_index

    def refresh_library_index(self, side: ServerKind, *, progress=None) -> TargetIndex:
        """Re-validate now, ignoring the reusable-token fast path (troubleshooting)."""
        return self.ensure_library_index(side, progress=progress, force=True)

    def rebuild_library_index(self, side: ServerKind, *, progress=None) -> TargetIndex:
        """Discard the disposable index (memory + persisted) and read it again.

        Never touches authoritative state (mappings, journals, snapshots, jobs).
        """
        key = side.value
        with self._index_locks[key]:
            self.index_cache.pop(key, None)
            self._index_generation.pop(key, None)
            self._index_validated_at.pop(key, None)
        if self.has_store:
            try:
                self.store.clear_library_index(
                    server_kind=side.value, server_identity=self.server_identity(side)
                )
            except Exception as exc:
                log.warning("%s index clear failed: %s", key, exc)
        return self.ensure_library_index(side, progress=progress, force=True)

    def build_index(self, side: ServerKind) -> TargetIndex:
        """Build (unvalidated) and cache one library index.

        Used by non-validating callers. Prefer :meth:`ensure_library_index`
        before planning so stale data cannot drive a decision.
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
            self._index_generation.setdefault(key, {})["validated"] = False
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
            gen = self._index_generation.get(key) or {}
            entry["generation"] = {
                "source": gen.get("source"),
                "validated": bool(gen.get("validated")),
                "fingerprint": (gen.get("fingerprint") or "")[:12] or None,
                "change_token": gen.get("change_token"),
                "algorithm_version": index_cache.INDEX_ALGORITHM_VERSION,
                "built_at": gen.get("built_at"),
            }
            entry["counters"] = dict(self._index_counters.get(key, {}))
            entry["stale"] = bool(status.get("stale"))
            snapshot[key] = entry
        return snapshot

    def invalidate_index(self, side: ServerKind | None = None) -> None:
        if side is None:
            self.index_cache.clear()
            self._index_generation.clear()
            self._index_validated_at.clear()
            for status in self.index_status.values():
                status.update(state="idle", tracks=0, finished_at=None, stale=False)
        else:
            key = side.value
            self.index_cache.pop(key, None)
            self._index_generation.pop(key, None)
            self._index_validated_at.pop(key, None)
            self.index_status.setdefault(key, {}).update(state="idle", tracks=0, stale=False)

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
