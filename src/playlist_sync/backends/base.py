"""Backend abstraction shared by Plex and Gonic clients."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field


class BackendError(RuntimeError):
    """Base class for backend failures."""


class BackendAuthError(BackendError):
    """Credentials were rejected."""


class BackendUnavailable(BackendError):
    """The server could not be reached (temporary outages land here)."""


class BackendCapabilityError(BackendError):
    """The backend cannot perform a requested operation."""


class PlaylistNotFound(BackendError):
    """The requested playlist does not exist on the backend."""


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

    def close(self) -> None:  # pragma: no cover - optional
        pass


class WritablePlaylistBackend(PlaylistBackend):
    """Extended interface for backends that can mutate playlists."""

    @abstractmethod
    def create_playlist(self, name: str, track_ids: Iterable[str]) -> str: ...

    @abstractmethod
    def replace_playlist_entries(self, playlist_id: str, track_ids: Iterable[str]) -> None:
        """Mirror ``track_ids`` (ordered, duplicates allowed) onto the playlist."""

    @abstractmethod
    def rename_playlist(self, playlist_id: str, name: str) -> None: ...

    @abstractmethod
    def delete_playlist(self, playlist_id: str) -> None: ...

    def supports_reorder(self) -> bool:
        return True

    def supports_empty_create(self) -> bool:
        return True
