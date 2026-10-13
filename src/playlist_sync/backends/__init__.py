"""Backend clients for Plex and Gonic."""

from .base import (
    BackendAuthError,
    BackendCapabilityError,
    BackendError,
    BackendUnavailable,
    CapabilityReport,
    PlaylistBackend,
    PlaylistNotFound,
    WritablePlaylistBackend,
)
from .gonic import GonicClient
from .plex import PlexClient, normalize_mbid

__all__ = [
    "BackendAuthError",
    "BackendCapabilityError",
    "BackendError",
    "BackendUnavailable",
    "CapabilityReport",
    "GonicClient",
    "PlaylistBackend",
    "PlaylistNotFound",
    "PlexClient",
    "WritablePlaylistBackend",
    "normalize_mbid",
]
