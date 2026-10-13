"""Domain models shared by every backend, the planner, and the UI.

The models are deliberately backend-neutral: a Plex track and a Gonic track both
become a :class:`RemoteTrack`. Playlist entries are modelled as an ordered
*sequence of occurrences* (not a set) so duplicates survive round-tripping.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ServerKind(str, Enum):
    PLEX = "plex"
    GONIC = "gonic"


class Direction(str, Enum):
    PLEX_TO_GONIC = "plex_to_gonic"
    GONIC_TO_PLEX = "gonic_to_plex"
    BIDIRECTIONAL = "bidirectional"


class BaselineChoice(str, Enum):
    PLEX_WINS = "plex_wins"
    GONIC_WINS = "gonic_wins"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class RemoteTrack:
    """A normalised view of a track on one server."""

    server: ServerKind
    track_id: str
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    disc_number: int | None = None
    track_number: int | None = None
    year: int | None = None
    duration_ms: int | None = None
    # Absolute file path as the owning server sees it (Plex always; Gonic may
    # expose a path relative to its music root).
    path: str | None = None
    # Path relative to the configured "own" music root, already normalised.
    rel_path: str | None = None
    musicbrainz_ids: frozenset[str] = frozenset()
    isrcs: frozenset[str] = frozenset()
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def identity_hint(self) -> str:
        """A short human label used in reports and logs (never for matching)."""
        bits = [self.artist or self.album_artist, self.title]
        label = " – ".join(b for b in bits if b)
        return label or self.track_id


@dataclass(frozen=True)
class PlaylistEntry:
    """One occurrence inside a playlist.

    ``occurrence_id`` is the backend-specific per-occurrence handle
    (Plex: ``playlistItemID``; Gonic: ``None``, addressed by position).
    """

    index: int
    track: RemoteTrack
    occurrence_id: str | None = None

    def fingerprint_token(self) -> str:
        return self.track.track_id


@dataclass(frozen=True)
class PlaylistSummary:
    server: ServerKind
    playlist_id: str
    name: str
    owner: str | None = None
    track_count: int | None = None
    is_smart: bool = False
    is_audio: bool = True
    updated_at: str | None = None
    # True if we believe the configured credentials can write to this playlist.
    writable: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class RemotePlaylist:
    summary: PlaylistSummary
    entries: tuple[PlaylistEntry, ...]

    @property
    def server(self) -> ServerKind:
        return self.summary.server

    @property
    def playlist_id(self) -> str:
        return self.summary.playlist_id

    def track_ids(self) -> list[str]:
        """Ordered track ids, duplicates retained."""
        return [e.track.track_id for e in self.entries]

    def fingerprint(self) -> str:
        """Order-sensitive fingerprint over *track ids only*.

        Track ids are stable backend ids, so this is suitable for persisted
        last-successful snapshots and change detection within one server.
        Cross-server fingerprints are computed after canonical matching.
        """
        h = hashlib.sha256()
        for entry in self.entries:
            h.update(entry.track.track_id.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def occurrences_by_track(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.track.track_id] = counts.get(entry.track.track_id, 0) + 1
        return counts


@dataclass
class MatchCandidate:
    track: RemoteTrack
    method: str
    score: float
    detail: str = ""


@dataclass
class MatchResult:
    """Outcome of matching one source entry to the target library."""

    match: RemoteTrack | None
    method: str
    status: str  # "matched" | "ambiguous" | "unmatched" | "override"
    candidates: list[MatchCandidate] = field(default_factory=list)
    detail: str = ""

    @property
    def is_confident(self) -> bool:
        return self.match is not None and self.status in {"matched", "override"}


@dataclass
class PlannedEntry:
    source_index: int
    source_track: RemoteTrack
    target_track: RemoteTrack | None
    match: MatchResult


def fingerprint_ids(track_ids: list[str]) -> str:
    """Order-sensitive fingerprint over a list of target track ids."""
    h = hashlib.sha256()
    for track_id in track_ids:
        h.update(str(track_id).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass
class Diff:
    """A human/UI diff between the current target and the planned target."""

    additions: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    # Stable destination track ids backing additions/removals (matching and
    # mutation always use these; ``additions``/``removals`` remain display labels).
    addition_ids: list[str] = field(default_factory=list)
    removal_ids: list[str] = field(default_factory=list)
    moves: list[str] = field(default_factory=list)
    renames: list[str] = field(default_factory=list)
    unchanged: int = 0
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    order_changed: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.additions or self.removals or self.moves or self.renames)
