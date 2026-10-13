"""Plex client built on python-plexapi.

Verified behaviours implemented here (see docs/api-notes.md):
- token auth only; no Plex cloud / myplex sign-in.
- ``playlists(playlistType='audio', sectionId=...)`` for discovery.
- playlist items are ordered and may repeat; each carries ``playlistItemID``.
- smart playlists are read-only.
- empty static playlist creation is *not* supported by plexapi and is blocked.
- writes use raw endpoints keyed by ``playlistItemID`` (never the ambiguous
  ratingKey-based helpers) so duplicate occurrences and order are preserved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

import plexapi.exceptions
from plexapi.server import PlexServer

from ..errors import AppError, make_error
from ..matching.paths import PathMapper
from ..models import PlaylistEntry, PlaylistSummary, RemotePlaylist, RemoteTrack, ServerKind
from ..redact import Secret, register_secret
from .base import (
    BackendAuthError,
    BackendCapabilityError,
    BackendError,
    BackendUnavailable,
    CapabilityReport,
    PlaylistNotFound,
    WritablePlaylistBackend,
)
from .resilience import CircuitBreaker, OutageLogger, RetryPolicy, with_retry

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_APPEND_CHUNK = 1000


def normalize_mbid(value: str | None) -> str:
    """Reduce a Plex/Gonic MusicBrainz identifier to its UUID for comparison."""
    if not value:
        return ""
    match = _UUID.search(value)
    if match:
        return match.group(0).casefold()
    return value.strip().casefold()


class PlexClient(WritablePlaylistBackend):
    kind = "plex"

    def __init__(
        self,
        base_url: str,
        token: str | Secret,
        *,
        path_mapper: PathMapper | None = None,
        library_sections: list[str] | None = None,
        timeout: float = 30.0,
        verify: bool | str = True,
        retry_attempts: int = 3,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 30.0,
        retry_total_seconds: float = 60.0,
        circuit_failure_threshold: int = 5,
        circuit_reset_seconds: float = 60.0,
        server_factory=None,
        session=None,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token if isinstance(token, Secret) else Secret(token)
        register_secret(self._token.get())
        self.path_mapper = path_mapper or PathMapper([])
        self.library_sections = list(library_sections or [])
        self._track_cache: dict[str, Any] = {}
        self._retry = RetryPolicy(
            attempts=max(1, retry_attempts),
            base=retry_base_seconds,
            max_delay=retry_max_seconds,
            total=retry_total_seconds,
            retry_after_max=retry_max_seconds * 4,
        )
        self._breaker = CircuitBreaker(
            "plex",
            failure_threshold=circuit_failure_threshold,
            reset_seconds=circuit_reset_seconds,
        )
        self._outages = OutageLogger("plex")

        import requests

        self._session = session or requests.Session()
        # The Plex token is a custom header that requests would forward to a
        # different origin on a redirect. Do not follow redirects; the operator
        # should configure the final Plex URL.
        self._session.max_redirects = 0
        if isinstance(verify, str):
            self._session.verify = verify
        elif verify is False:
            self._session.verify = False
        if server_factory is None:
            server_factory = PlexServer
        try:
            self._server = server_factory(
                self.base_url, self._token.get(), session=self._session, timeout=timeout
            )
        except Exception as exc:  # plexapi raises a variety of request errors
            raise self._classify(exc) from exc

    # -- error mapping ---------------------------------------------------------

    @staticmethod
    def _classify(exc: Exception) -> BackendError:
        text = str(exc)
        if "TooManyRedirects" in type(exc).__name__ or "Redirect" in type(exc).__name__:
            return make_error(
                "HTTP_REDIRECT_LOOP",
                "Plex redirected the request; redirects are not followed to protect the token. "
                "Use the final Plex URL (no redirect) in Settings.",
            )
        if isinstance(exc, plexapi.exceptions.Unauthorized):
            return BackendAuthError("plex rejected the token")
        if isinstance(
            exc,
            (plexapi.exceptions.NotFound, plexapi.exceptions.BadRequest),
        ) and "unauthorized" in text.lower():
            return BackendAuthError("plex rejected the token")
        if isinstance(exc, plexapi.exceptions.NotFound):
            return BackendError(text)
        if "Connection" in type(exc).__name__ or "Timeout" in type(exc).__name__:
            return BackendUnavailable(f"plex unreachable: {type(exc).__name__}")
        return BackendError(f"plex error: {text}")

    def _read(self, fn):
        """Run a read-only Plex call with bounded retry, classification and breaker."""

        def call():
            try:
                return fn()
            except AppError:
                raise
            except Exception as exc:
                raise self._classify(exc) from exc

        return with_retry(
            call, self._retry, breaker=self._breaker, outages=self._outages, idempotent=True
        )

    # -- capabilities ----------------------------------------------------------

    def doctor(self) -> CapabilityReport:
        report = CapabilityReport(kind=self.kind, reachable=True, authenticated=True)
        try:
            report.server_version = str(getattr(self._server, "version", "") or "")
            report.features = {
                "token_auth": True,
                "smart_playlists_readonly": True,
                "create_empty": False,
                "playlist_item_occurrence_ids": True,
                "reorder_via_move": True,
            }
            report.discovered["friendly_name"] = getattr(self._server, "friendlyName", "")
            report.discovered["machine_identifier"] = getattr(self._server, "machineIdentifier", "")
            sections = [
                {"key": s.key, "title": s.title, "type": s.type}
                for s in self._server.library.sections()
                if getattr(s, "type", "") == "artist"
            ]
            report.discovered["music_sections"] = sections
            if not sections:
                report.warnings.append("no Plex music library sections were found")
            report.discovered["playlist_count"] = len(self.list_playlists())
        except Exception as exc:
            report.errors.append(str(exc))
        return report

    def capabilities(self) -> dict[str, bool]:
        return {
            "can_create_empty": False,
            "can_reorder": True,
            "supports_duplicates": True,
            "smart_playlists": True,
        }

    # -- discovery -------------------------------------------------------------

    def _music_sections(self) -> list[Any]:
        sections = []
        for section in self._server.library.sections():
            if getattr(section, "type", "") != "artist":
                continue
            if self.library_sections and str(section.title) not in self.library_sections and str(
                section.key
            ) not in self.library_sections:
                continue
            sections.append(section)
        return sections

    def list_playlists(self, *, audio_only: bool = True) -> list[PlaylistSummary]:
        sections = self._music_sections()
        playlists: dict[str, Any] = {}

        def _fetch():
            if sections:
                for section in sections:
                    for playlist in self._server.playlists(
                        playlistType="audio", sectionId=section.key
                    ):
                        playlists[str(playlist.ratingKey)] = playlist
            else:
                for playlist in self._server.playlists(playlistType="audio" if audio_only else None):
                    playlists[str(playlist.ratingKey)] = playlist

        self._read(_fetch)
        return [self._summarize(playlist) for playlist in playlists.values()]

    def _summarize(self, playlist: Any) -> PlaylistSummary:
        return PlaylistSummary(
            server=ServerKind.PLEX,
            playlist_id=str(playlist.ratingKey),
            name=playlist.title,
            owner=None,  # not exposed by plexapi (see docs/api-notes.md)
            track_count=playlist.leafCount,
            is_smart=bool(playlist.smart),
            is_audio=playlist.playlistType == "audio",
            updated_at=playlist.updatedAt.isoformat() if playlist.updatedAt else None,
            writable=not bool(playlist.smart),
            extra={"guid": playlist.guid, "playlist_type": playlist.playlistType},
        )

    def _playlist_obj(self, playlist_id: str) -> Any:
        def _fetch():
            try:
                return self._server.fetchItem(f"/playlist/{playlist_id}")
            except plexapi.exceptions.NotFound:
                # Some servers/permissions do not allow fetching a playlist by its
                # key even though it is listed. Fall back to the listed playlists.
                try:
                    for candidate in self._server.playlists():
                        if str(getattr(candidate, "ratingKey", "")) == str(playlist_id):
                            return candidate
                except Exception:
                    pass
                raise PlaylistNotFound(f"plex playlist {playlist_id!r} not found") from None

        return self._read(_fetch)

    def get_playlist(self, playlist_id: str) -> RemotePlaylist:
        playlist = self._playlist_obj(playlist_id)
        if playlist.smart:
            # Evaluating a smart playlist's contents is a read; safe.
            pass
        entries: list[PlaylistEntry] = []
        for index, item in enumerate(playlist.items()):
            track = self._track_to_remote(item)
            entries.append(
                PlaylistEntry(
                    index=index,
                    track=track,
                    occurrence_id=str(item.playlistItemID) if item.playlistItemID else None,
                )
            )
        return RemotePlaylist(summary=self._summarize(playlist), entries=tuple(entries))

    # -- library index ---------------------------------------------------------

    def iter_library_tracks(self) -> Iterator[RemoteTrack]:
        sections = self._music_sections()
        if not sections:
            raise BackendCapabilityError(
                "no Plex music sections configured/found; set plex.library_sections"
            )
        for section in sections:
            for track in self._read(section.searchTracks):
                self._track_cache[str(track.ratingKey)] = track
                yield self._track_to_remote(track)

    # -- conversion ------------------------------------------------------------

    def _track_to_remote(self, track: Any) -> RemoteTrack:
        self._track_cache[str(track.ratingKey)] = track
        path = None
        try:
            locations = track.locations
            if not locations:
                track = self._reload_track(track)
                locations = track.locations
            path = locations[0] if locations else None
        except Exception:
            path = None

        rel_candidates = self.path_mapper.strip_plex(path) if path else []
        rel_path = rel_candidates[0] if len(rel_candidates) == 1 else None

        mbids: set[str] = set()
        try:
            for guid in track.guids:
                normalised = normalize_mbid(guid.id)
                if normalised:
                    mbids.add(normalised)
        except Exception:
            pass

        return RemoteTrack(
            server=ServerKind.PLEX,
            track_id=str(track.ratingKey),
            title=track.title or "",
            artist=getattr(track, "originalTitle", None) or getattr(track, "grandparentTitle", "") or "",
            album_artist=getattr(track, "grandparentTitle", "") or "",
            artists=tuple(
                v
                for v in [getattr(track, "originalTitle", None) or getattr(track, "grandparentTitle", None)]
                if v
            ),
            album=getattr(track, "parentTitle", "") or "",
            disc_number=getattr(track, "parentIndex", None),
            track_number=getattr(track, "index", None),
            year=getattr(track, "year", None),
            duration_ms=getattr(track, "duration", None),
            path=path,
            rel_path=rel_path,
            musicbrainz_ids=frozenset(mbids),
            isrcs=frozenset(),
            extra={"ambiguous_path_roots": rel_candidates if len(rel_candidates) > 1 else []},
        )

    def _reload_track(self, track: Any) -> Any:
        key = str(track.ratingKey)
        cached = self._track_cache.get(key)
        if cached is not None and getattr(cached, "media", None):
            return cached
        try:
            reloaded = self._server.fetchItem(f"/library/metadata/{key}")
            self._track_cache[key] = reloaded
            return reloaded
        except Exception:
            return track

    # -- writing ---------------------------------------------------------------

    def _append_ids(self, playlist: Any, rating_keys: list[str]) -> None:
        for start in range(0, len(rating_keys), _APPEND_CHUNK):
            chunk = rating_keys[start : start + _APPEND_CHUNK]
            uri = f"{self._server._uriRoot()}/library/metadata/{','.join(chunk)}"
            self._server.query(f"{playlist.key}/items?uri={uri}", method=self._server._session.put)

    def _remove_occurrence(self, playlist: Any, occurrence_id: str) -> None:
        self._server.query(
            f"{playlist.key}/items/{occurrence_id}", method=self._server._session.delete
        )

    def _move_after(self, playlist: Any, occurrence_id: str, after_id: str | None) -> None:
        path = f"{playlist.key}/items/{occurrence_id}/move"
        if after_id:
            path += f"?after={after_id}"
        self._server.query(path, method=self._server._session.put)

    def create_playlist(self, name: str, track_ids: Iterable[str]) -> str:
        rating_keys = [str(x) for x in track_ids]
        if not rating_keys:
            raise BackendCapabilityError(
                "Plex cannot create an empty playlist via the API; refusing to create one"
            )
        head = rating_keys[:_APPEND_CHUNK]
        uri = f"{self._server._uriRoot()}/library/metadata/{','.join(head)}"
        from plexapi.utils import joinArgs

        key = f"/playlists{joinArgs({'uri': uri, 'type': 'audio', 'title': name, 'smart': 0})}"
        result = self._server.query(key, method=self._server._session.post)
        first = result[0]
        rating_key = getattr(first, "ratingKey", None)
        if rating_key is None:
            rating_key = first.attrib.get("ratingKey")
        if rating_key is None:
            raise BackendError("Plex did not return a ratingKey for the created playlist")
        playlist = self._playlist_obj(str(rating_key))
        if len(rating_keys) > _APPEND_CHUNK:
            self._append_ids(playlist, rating_keys[_APPEND_CHUNK:])
        return str(playlist.ratingKey)

    def replace_playlist_entries(self, playlist_id: str, track_ids: Iterable[str]) -> None:
        playlist = self._playlist_obj(playlist_id)
        if playlist.smart:
            raise BackendCapabilityError("refusing to write to a Plex smart playlist")
        desired = [str(x) for x in track_ids]
        current = [
            (str(item.playlistItemID), str(item.ratingKey))
            for item in playlist.items()
            if item.playlistItemID is not None
        ]
        self._reconcile(playlist, current, desired)

    def append_entries(self, playlist_id: str, track_ids: Iterable[str]) -> None:
        playlist = self._playlist_obj(playlist_id)
        if playlist.smart:
            raise BackendCapabilityError("refusing to write to a Plex smart playlist")
        ids = [str(x) for x in track_ids]
        if ids:
            self._append_ids(playlist, ids)

    # -- reconciliation --------------------------------------------------------

    @staticmethod
    def _select_kept(current_rids: list[str], desired: list[str]) -> tuple[list[int], list[int]]:
        """Greedy maximum in-order common subsequence.

        Returns (kept_current_indices, missing_desired_indices). Choosing the
        earliest unused matching position after the cursor is optimal for LCS
        and is deterministic, which keeps writes explainable.
        """
        positions: dict[str, list[int]] = {}
        for idx, rid in enumerate(current_rids):
            positions.setdefault(rid, []).append(idx)

        kept: list[int] = []
        missing: list[int] = []
        cursor = -1
        for desired_index, rid in enumerate(desired):
            candidate = None
            for pos in positions.get(rid, ()):  # already in ascending order
                if pos > cursor:
                    candidate = pos
                    break
            if candidate is None:
                missing.append(desired_index)
            else:
                kept.append(candidate)
                cursor = candidate
        return kept, missing

    def _reconcile(self, playlist: Any, current: list[tuple[str, str]], desired: list[str]) -> None:
        current_rids = [rid for _, rid in current]
        kept_indices, missing_indices = self._select_kept(current_rids, desired)
        kept_set = set(kept_indices)

        # 1. remove occurrences we will not keep
        for idx, (occ_id, _rid) in enumerate(current):
            if idx not in kept_set:
                self._remove_occurrence(playlist, occ_id)

        # 2. append the missing desired occurrences at the end, in order
        if missing_indices:
            self._append_ids(playlist, [desired[i] for i in missing_indices])

        # 3. reorder to the exact desired sequence
        self._reorder(playlist, desired)

    def _reorder(self, playlist: Any, desired: list[str]) -> None:
        # Re-read to learn the playlistItemIDs of appended occurrences.
        fresh = [
            (str(item.playlistItemID), str(item.ratingKey))
            for item in playlist.reload().items()
            if item.playlistItemID is not None
        ]
        work = [occ for occ, _ in fresh]
        rids = [rid for _, rid in fresh]
        if rids == desired:
            return

        # map each rid to the queue of occurrence ids currently available
        queues: dict[str, list[str]] = {}
        for occ, rid in fresh:
            queues.setdefault(rid, []).append(occ)

        target: list[str] = []
        for rid in desired:
            target.append(queues[rid].pop(0))

        for i in range(len(target)):
            if work[i] == target[i]:
                continue
            occ = target[i]
            j = work.index(occ)
            # simulate the move locally
            work.pop(j)
            work.insert(i, occ)
            after = work[i - 1] if i > 0 else None
            self._move_after(playlist, occ, after)

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        playlist = self._playlist_obj(playlist_id)
        if playlist.smart:
            raise BackendCapabilityError("refusing to rename a Plex smart playlist")
        playlist.editTitle(name)

    def delete_playlist(self, playlist_id: str) -> None:
        playlist = self._playlist_obj(playlist_id)
        playlist.delete()

    def supports_empty_create(self) -> bool:
        return False

    def close(self) -> None:  # pragma: no cover
        import contextlib

        with contextlib.suppress(Exception):
            self._session.close()
