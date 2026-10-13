"""Gonic (Subsonic / OpenSubsonic) HTTP client.

Verified behaviours implemented here (see docs/api-notes.md):
- require ``u`` + ``c``; token auth ``t = md5(password + salt)`` with a fresh
  random salt, or password auth ``p``.
- ``getPlaylists`` / ``getPlaylist`` (``entry`` list) — ordered, duplicates kept.
- ``createPlaylist`` with ``playlistId`` **replaces** the whole ordered list;
  repeated ``songId`` params preserve order.
- ``updatePlaylist`` for name/comment/public and append/remove-by-index.
- JSON responses (``f=json``), error codes in ``subsonic-response.error``.

Resilience: reads are retried with bounded backoff; mutations are never
auto-retried; a per-backend circuit breaker and outage logger avoid request/log
storms; response bytes are capped; pagination loops are detected.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import urlencode

import httpx

from ..errors import AppError, make_error
from ..matching.paths import PathMapper
from ..models import PlaylistEntry, PlaylistSummary, RemotePlaylist, RemoteTrack, ServerKind
from ..redact import Secret, register_secret, scrub
from .base import (
    BackendAuthError,
    BackendCapabilityError,
    BackendError,
    BackendUnavailable,
    CapabilityReport,
    PlaylistNotFound,
    WritablePlaylistBackend,
)
from .resilience import CircuitBreaker, OutageLogger, RetryPolicy, parse_retry_after, with_retry

CLIENT_NAME = "plex-gonic-playlist-sync"
API_VERSION = "1.16.1"
PAGE_SIZE = 500


class GonicClient(WritablePlaylistBackend):
    kind = "gonic"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str | Secret,
        *,
        path_mapper: PathMapper | None = None,
        music_folder_id: str | None = None,
        timeout: float = 30.0,
        verify: bool | str = True,
        use_token_auth: bool = True,
        chunk_size: int = 1000,
        max_response_bytes: int = 64 * 1024 * 1024,
        max_playlist_entries: int = 100_000,
        max_pages: int = 10_000,
        max_page_repeats: int = 3,
        retry_attempts: int = 3,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 30.0,
        retry_total_seconds: float = 60.0,
        circuit_failure_threshold: int = 5,
        circuit_reset_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password if isinstance(password, Secret) else Secret(password)
        register_secret(self._password.get())
        self.path_mapper = path_mapper or PathMapper([])
        self.music_folder_id = music_folder_id
        self.use_token_auth = use_token_auth
        self.chunk_size = max(1, chunk_size)
        self.max_response_bytes = max_response_bytes
        self.max_playlist_entries = max_playlist_entries
        self.max_pages = max_pages
        self.max_page_repeats = max(1, max_page_repeats)
        self._retry = RetryPolicy(
            attempts=max(1, retry_attempts),
            base=retry_base_seconds,
            max_delay=retry_max_seconds,
            total=retry_total_seconds,
            retry_after_max=retry_max_seconds * 4,
        )
        self._breaker = CircuitBreaker(
            "gonic",
            failure_threshold=circuit_failure_threshold,
            reset_seconds=circuit_reset_seconds,
        )
        self._outages = OutageLogger("gonic")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            verify=verify,
            transport=transport,
            headers={"User-Agent": CLIENT_NAME},
        )

    # -- low level -------------------------------------------------------------

    def _auth_params(self) -> dict[str, str]:
        if self.use_token_auth:
            salt = secrets.token_hex(8)
            token = hashlib.md5(
                (self._password.get() + salt).encode("utf-8")
            ).hexdigest()
            return {"u": self.username, "t": token, "s": salt}
        return {"u": self.username, "p": self._password.get()}

    def _common_params(self) -> dict[str, str]:
        return {"c": CLIENT_NAME, "v": API_VERSION, "f": "json"}

    @property
    def circuit_state(self) -> str:
        return self._breaker.state

    def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        list_params=None,
        idempotent: bool | None = None,
    ) -> dict[str, Any]:
        payload = {**self._common_params(), **self._auth_params()}
        if params:
            payload.update({k: v for k, v in params.items() if v is not None})
        encoded = [(key, str(value)) for key, value in payload.items()]
        if list_params:
            for key, values in list_params:
                encoded.extend((key, str(v)) for v in values)

        if idempotent is None:
            idempotent = method == "GET"
        call = lambda: self._send(endpoint, method, encoded)  # noqa: E731
        if idempotent:
            return with_retry(
                call, self._retry, breaker=self._breaker, outages=self._outages, idempotent=True
            )
        return with_retry(
            call, self._retry, breaker=self._breaker, outages=self._outages, idempotent=False
        )

    def _send(self, endpoint: str, method: str, encoded: list[tuple[str, str]]) -> dict[str, Any]:
        url = f"/rest/{endpoint}"
        try:
            if method == "POST":
                body = urlencode(encoded)
                ctx = self._client.stream(
                    "POST",
                    url,
                    content=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            else:
                ctx = self._client.stream("GET", url, params=encoded)
            with ctx as response:
                raw = self._read_capped(response)
                status = response.status_code
                headers = response.headers
        except httpx.TimeoutException as exc:
            raise BackendUnavailable(
                f"gonic timed out: {type(exc).__name__}", code="CONNECT_TIMEOUT"
            ) from exc
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise self._connect_error(exc) from exc
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"gonic request failed: {type(exc).__name__}") from exc
        return self._parse(endpoint, status, headers, raw)

    @staticmethod
    def _connect_error(exc: Exception) -> BackendUnavailable:
        text = f"{type(exc).__name__}: {exc}".lower()
        if "name or service" in text or "nodename" in text or "getaddrinfo" in text or "dns" in text:
            return BackendUnavailable("gonic host could not be resolved", code="CONNECT_DNS")
        if "refused" in text:
            return BackendUnavailable("gonic refused the connection", code="CONNECT_REFUSED")
        return BackendUnavailable("gonic is unreachable", code="CONNECT_HTTP")

    def _read_capped(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self.max_response_bytes:
                raise make_error(
                    "RESPONSE_TOO_LARGE",
                    "The Gonic response exceeded the configured size limit.",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _parse(
        self, endpoint: str, status: int, headers: httpx.Headers, raw: bytes
    ) -> dict[str, Any]:
        if status >= 400:
            raise self._status_error(status, headers)
        head = raw.lstrip()[:64].lower()
        if head.startswith(b"<") or b"<html" in raw[:512].lower():
            raise make_error(
                "RESPONSE_NOT_API",
                "Gonic returned a web page instead of API data (check the URL/auth).",
            )
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise make_error(
                "RESPONSE_MALFORMED", "Gonic returned a malformed response."
            ) from exc
        root = body.get("subsonic-response") if isinstance(body, dict) else None
        if not isinstance(root, dict):
            raise make_error(
                "RESPONSE_MALFORMED", "Gonic response is missing the subsonic-response element."
            )
        if root.get("status") != "ok":
            error = root.get("error") or {}
            code = error.get("code")
            message = scrub(str(error.get("message", "unknown error")))
            if code == 40:
                raise BackendAuthError("Gonic rejected the credentials.", safe_message="Gonic rejected the credentials.")
            if code == 70:
                raise PlaylistNotFound("gonic: the playlist was not found")
            if code == 50:
                raise BackendCapabilityError("gonic: not authorized for this operation")
            err = make_error(
                "SUBSONIC_ERROR", f"Gonic reported an error (code {code}): {message}"
            )
            err.details["subsonic_code"] = code
            raise err
        return root

    @staticmethod
    def _status_error(status: int, headers: httpx.Headers) -> AppError:
        retry_after = parse_retry_after(headers.get("Retry-After"))
        if status == 401:
            return BackendAuthError("Gonic rejected the credentials.")
        if status == 403:
            return BackendError(
                "Gonic denied the request.", code="HTTP_FORBIDDEN"
            )
        if status == 429:
            err = make_error("HTTP_RATE_LIMIT", "Gonic rate-limited the request.")
            err.retry_after = retry_after
            return err
        if status == 503:
            return BackendError("Gonic is in maintenance.", code="HTTP_MAINTENANCE")
        if status >= 500:
            return BackendError("Gonic returned a server error.", code="HTTP_SERVER_ERROR")
        return BackendError(f"Gonic returned HTTP {status}.", code="CONNECT_HTTP")

    # -- capabilities ----------------------------------------------------------

    def doctor(self) -> CapabilityReport:
        report = CapabilityReport(kind=self.kind, reachable=False, authenticated=False)
        try:
            root = self._request("ping")
        except BackendAuthError as exc:
            report.errors.append(str(exc))
            report.reachable = True
            return report
        except BackendUnavailable as exc:
            report.errors.append(str(exc))
            return report
        except BackendError as exc:
            report.errors.append(str(exc))
            report.reachable = True
            return report

        report.reachable = True
        report.authenticated = True
        report.server_version = str(root.get("version", ""))
        report.open_subsonic = bool(root.get("openSubsonic"))
        report.features = {
            "token_auth": True,
            "create_playlist": True,
            "update_playlist": True,
            "delete_playlist": True,
            "playlist_duplicates": True,
            "playlist_reorder_via_replace": True,
        }
        try:
            folders = self._request("getMusicFolders").get("musicFolders", {}).get("musicFolder", [])
            report.discovered["music_folders"] = [f.get("name") for f in folders]
        except BackendError as exc:
            report.warnings.append(f"getMusicFolders failed: {exc}")
        try:
            playlists = self.list_playlists(audio_only=False)
            report.discovered["playlist_count"] = len(playlists)
        except BackendError as exc:
            report.warnings.append(f"list_playlists failed: {exc}")
        if not report.open_subsonic:
            report.warnings.append("server does not advertise OpenSubsonic extensions")
        return report

    def capabilities(self) -> dict[str, bool]:
        return {
            "can_create_empty": True,
            "can_reorder": True,
            "supports_duplicates": True,
            "smart_playlists": False,
        }

    # -- reading ---------------------------------------------------------------

    def _playlist_summary(self, raw: dict[str, Any], *, writable: bool | None = None) -> PlaylistSummary:
        return PlaylistSummary(
            server=ServerKind.GONIC,
            playlist_id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            owner=raw.get("owner"),
            track_count=raw.get("songCount"),
            is_smart=False,
            is_audio=True,
            updated_at=raw.get("changed") or raw.get("created"),
            writable=writable,
            extra={"public": raw.get("public", False), "comment": raw.get("comment", "")},
        )

    def list_playlists(self, *, audio_only: bool = True) -> list[PlaylistSummary]:
        root = self._request("getPlaylists")
        raw_list = (root.get("playlists") or {}).get("playlist") or []
        out: list[PlaylistSummary] = []
        for raw in raw_list:
            # Only the owner can mutate a Gonic playlist (verified from source).
            owned = raw.get("owner") == self.username
            out.append(self._playlist_summary(raw, writable=owned))
        return out

    def get_playlist(self, playlist_id: str) -> RemotePlaylist:
        root = self._request("getPlaylist", {"id": playlist_id})
        raw = root.get("playlist")
        if not raw:
            raise PlaylistNotFound(f"gonic playlist {playlist_id!r} not found")
        raw_entries = raw.get("entry") or []
        if len(raw_entries) > self.max_playlist_entries:
            err = make_error(
                "RESOURCE_LIMIT",
                f"Gonic playlist has {len(raw_entries)} entries, above the configured limit "
                f"({self.max_playlist_entries}); the write is blocked.",
            )
            raise err
        entries: list[PlaylistEntry] = []
        for index, song in enumerate(raw_entries):
            track = self._track_from_subsonic(song)
            entries.append(PlaylistEntry(index=index, track=track, occurrence_id=None))
        summary = self._playlist_summary(raw, writable=raw.get("owner") == self.username)
        expected = raw.get("songCount")
        if expected is not None and expected != len(entries):
            summary = PlaylistSummary(
                **{**summary.__dict__, "extra": {**summary.extra, "read_warning": (
                    f"server reported songCount={expected} but {len(entries)} entries were "
                    "resolvable; unresolvable entries are dropped on read"
                )}}
            )
        return RemotePlaylist(summary=summary, entries=tuple(entries))

    def _track_from_subsonic(self, song: dict[str, Any]) -> RemoteTrack:
        artists = tuple(
            a.get("name", "") for a in (song.get("artists") or []) if isinstance(a, dict)
        )
        album_artists = tuple(
            a.get("name", "") for a in (song.get("albumArtists") or []) if isinstance(a, dict)
        )
        duration_s = song.get("duration")
        duration_ms = int(duration_s) * 1000 if isinstance(duration_s, (int, float)) else None
        raw_path = song.get("path") or None
        rel_path = self.path_mapper.gonic_rel(raw_path) if raw_path else None
        mbids = frozenset(
            v.casefold() for v in [song.get("musicBrainzId")] if isinstance(v, str) and v
        )
        isrcs = frozenset(str(v).casefold() for v in (song.get("isrc") or []) if v)
        return RemoteTrack(
            server=ServerKind.GONIC,
            track_id=str(song.get("id", "")),
            title=str(song.get("title", "")),
            artist=str(song.get("displayArtist") or song.get("artist") or ""),
            album_artist=str(
                song.get("displayAlbumArtist")
                or (album_artists[0] if album_artists else "")
                or song.get("artist")
                or ""
            ),
            artists=artists or tuple([song.get("artist")] if song.get("artist") else []),
            album=str(song.get("album", "")),
            disc_number=song.get("discNumber"),
            track_number=song.get("track"),
            year=song.get("year"),
            duration_ms=duration_ms,
            path=raw_path,
            rel_path=rel_path,
            musicbrainz_ids=mbids,
            isrcs=isrcs,
            extra={"suffix": song.get("suffix"), "content_type": song.get("contentType")},
        )

    def iter_library_tracks(self) -> Iterator[RemoteTrack]:
        """Enumerate the library via paginated album lists, then per-album songs."""
        offset = 0
        pages = 0
        repeats = 0
        last_signature: tuple[str, ...] | None = None
        while True:
            root = self._request(
                "getAlbumList2",
                {
                    "type": "alphabeticalByName",
                    "size": PAGE_SIZE,
                    "offset": offset,
                    "musicFolderId": self.music_folder_id,
                },
            )
            albums = (root.get("albumList2") or {}).get("album") or []
            if not albums:
                break
            pages += 1
            if pages > self.max_pages:
                raise make_error(
                    "PAGINATION_LOOP",
                    f"Gonic exceeded {self.max_pages} pages while listing the library.",
                )
            signature = tuple(str(a.get("id")) for a in albums)
            if signature == last_signature:
                repeats += 1
                if repeats >= self.max_page_repeats:
                    raise make_error(
                        "PAGINATION_LOOP",
                        "Gonic repeated the same library page; results were not treated as complete.",
                    )
            else:
                repeats = 0
            last_signature = signature
            for album in albums:
                album_id = album.get("id")
                if not album_id:
                    continue
                album_root = self._request("getAlbum", {"id": album_id})
                album_data = album_root.get("album") or {}
                for song in album_data.get("song") or []:
                    yield self._track_from_subsonic(song)
            if len(albums) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

    # -- writing ---------------------------------------------------------------

    def create_playlist(self, name: str, track_ids: Iterable[str]) -> str:
        root = self._create_or_replace(track_ids, name=name)
        playlist = root.get("playlist") or {}
        playlist_id = playlist.get("id")
        if not playlist_id:
            raise BackendError("gonic did not return the created playlist id")
        return str(playlist_id)

    def replace_playlist_entries(self, playlist_id: str, track_ids: Iterable[str]) -> None:
        """Full ordered replace via createPlaylist(playlistId, songId...).

        Long lists are chunked: the first chunk replaces the playlist, remaining
        chunks are appended via ``updatePlaylist songIdToAdd`` in order, which
        preserves exact ordering and duplicates.
        """
        self._create_or_replace(track_ids, playlist_id=playlist_id)

    def append_entries(self, playlist_id: str, track_ids: Iterable[str]) -> None:
        """Append entries at the end via ``updatePlaylist songIdToAdd``."""
        ids = [str(x) for x in track_ids]
        for start in range(0, len(ids), self.chunk_size):
            chunk = ids[start : start + self.chunk_size]
            self._request(
                "updatePlaylist",
                {"playlistId": playlist_id},
                method="POST",
                list_params=[("songIdToAdd", chunk)],
            )

    def _create_or_replace(
        self, track_ids: Iterable[str], *, name: str | None = None, playlist_id: str | None = None
    ) -> dict:
        ids = [str(x) for x in track_ids]
        first, rest = ids[: self.chunk_size], ids[self.chunk_size :]
        params: dict[str, Any] = {}
        if playlist_id:
            params["playlistId"] = playlist_id
        if name is not None:
            params["name"] = name
        root = self._request(
            "createPlaylist", params, method="POST", list_params=[("songId", first)]
        )
        for start in range(0, len(rest), self.chunk_size):
            chunk = rest[start : start + self.chunk_size]
            self._request(
                "updatePlaylist",
                {"playlistId": playlist_id or (root.get("playlist") or {}).get("id")},
                method="POST",
                list_params=[("songIdToAdd", chunk)],
            )
        return root

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        self._request("updatePlaylist", {"playlistId": playlist_id, "name": name}, method="POST")

    def delete_playlist(self, playlist_id: str) -> None:
        self._request("deletePlaylist", {"id": playlist_id}, method="POST")

    def supports_reorder(self) -> bool:
        return True

    def supports_empty_create(self) -> bool:
        return True

    def close(self) -> None:
        self._client.close()
