"""Backend abstraction shared by Plex and Gonic clients.

Backend errors are typed :class:`~playlist_sync.errors.AppError` subclasses so a
stable machine code and retryability survive up to the API/UI boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ..errors import AppError


class BackendError(AppError):
    def __init__(self, message: str, *, code: str = "SUBSONIC_ERROR", safe_message: str | None = None, **kwargs):
        super().__init__(code=code, message=safe_message or message, **kwargs)


class BackendAuthError(BackendError):
    def __init__(self, message: str, safe_message: str | None = None):
        super().__init__(message, code="HTTP_AUTH", safe_message=safe_message)


class BackendUnavailable(BackendError):
    def __init__(self, message: str, *, code: str = "CONNECT_HTTP", safe_message: str | None = None):
        super().__init__(message, code=code, safe_message=safe_message)


class BackendCapabilityError(BackendError):
    def __init__(self, message: str, *, code: str = "CAPABILITY_UNSUPPORTED"):
        super().__init__(message, code=code)


class PlaylistNotFound(BackendError):
    def __init__(self, message: str):
        super().__init__(message, code="PLAYLIST_NOT_FOUND")


@dataclass
class CapabilityReport:
    kind: str
    reachable: bool
    authenticated: bool
    detail: str = ""
    server_version: str = ""
    open_subsonic: bool = False
    features: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Environment facts discovered while probing (e.g. music folders).
    discovered: dict[str, object] = field(default_factory=dict)


class PlaylistBackend(ABC):
    kind: str = "unknown"

    # -- capabilities ---------------------------------------------------------

    @abstractmethod
    def doctor(self) -> CapabilityReport: ...

    @abstractmethod
    def capabilities(self) -> dict[str, bool]:
        """Feature flags used by the planner (e.g. can_create_empty)."""

    # -- reading --------------------------------------------------------------

    @abstractmethod
    def list_playlists(self, *, audio_only: bool = True) -> list: ...

    @abstractmethod
    def get_playlist(self, playlist_id: str): ...

    @abstractmethod
    def iter_library_tracks(self) -> Iterator: ...

    def library_change_token(self) -> str | None:
        """A cheap, trustworthy signal that the library may have changed.

        Returns ``None`` when the backend exposes no reliable signal. A non-None
        value must only be treated as *possibly changed*: an unchanged token lets
        callers skip a re-read, but a changed token never proves the contents
        changed, and an unchanged token must never be inferred from counts.
        """
        return None

    def close(self) -> None:  # pragma: no cover - optional
        pass


class WritablePlaylistBackend(PlaylistBackend):
    """Extended interface for backends that can mutate playlists."""

    @abstractmethod
    def create_playlist(self, name: str, track_ids: Iterable[str]) -> str: ...

    @abstractmethod
    def replace_playlist_entries(self, playlist_id: str, track_ids: Iterable[str]) -> None:
        """Mirror ``track_ids`` (ordered, duplicates allowed) onto the playlist."""

    def append_entries(self, playlist_id: str, track_ids: Iterable[str]) -> None:
        """Append entries to the end of the playlist, preserving existing entries."""
        raise BackendCapabilityError("this backend does not support appending entries")

    @abstractmethod
    def rename_playlist(self, playlist_id: str, name: str) -> None: ...

    @abstractmethod
    def delete_playlist(self, playlist_id: str) -> None: ...

    def supports_reorder(self) -> bool:
        return True

    def supports_empty_create(self) -> bool:
        return True
