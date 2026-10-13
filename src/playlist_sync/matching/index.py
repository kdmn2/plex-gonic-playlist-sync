"""Cached, paginated index of a target library.

Searching the target once per source track does not scale, so the index is built
once per run (or reused from a cached snapshot) and answers O(1) lookups by
path, MusicBrainz id, ISRC, and normalised metadata.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from collections.abc import Callable, Iterable

from ..models import RemoteTrack
from .normalize import artist_key, normalize_text


def metadata_key(track: RemoteTrack) -> tuple[str, str, str, int, int]:
    return (
        artist_key(track.artists, track.album_artist or track.artist),
        normalize_text(track.title),
        normalize_text(track.album),
        int(track.disc_number or 0),
        int(track.track_number or 0),
    )


def fuzzy_label(track: RemoteTrack) -> str:
    return normalize_text(f"{track.album_artist or track.artist} {track.title}")


class TargetIndex:
    def __init__(self) -> None:
        self.by_path: dict[str, list[RemoteTrack]] = defaultdict(list)
        self.by_mbid: dict[str, list[RemoteTrack]] = defaultdict(list)
        self.by_isrc: dict[str, list[RemoteTrack]] = defaultdict(list)
        self.by_metadata: dict[tuple, list[RemoteTrack]] = defaultdict(list)
        self.by_id: dict[str, RemoteTrack] = {}
        self._tracks: list[RemoteTrack] = []
        self._fuzzy: dict[str, list[RemoteTrack]] = defaultdict(list)
        self.source: str = ""

    def add(self, track: RemoteTrack) -> None:
        self._tracks.append(track)
        self.by_id[track.track_id] = track
        if track.rel_path:
            self.by_path[track.rel_path].append(track)
        for mbid in track.musicbrainz_ids:
            self.by_mbid[mbid.casefold()].append(track)
        for isrc in track.isrcs:
            self.by_isrc[isrc.casefold()].append(track)
        self.by_metadata[metadata_key(track)].append(track)
        label = fuzzy_label(track)
        if label:
            self._fuzzy[label].append(track)

    def extend(self, tracks: Iterable[RemoteTrack]) -> None:
        for track in tracks:
            self.add(track)

    @property
    def size(self) -> int:
        return len(self._tracks)

    @property
    def tracks(self) -> list[RemoteTrack]:
        return list(self._tracks)

    def get(self, track_id: str) -> RemoteTrack | None:
        return self.by_id.get(track_id)

    def path_candidates(self, rel_path: str) -> list[RemoteTrack]:
        return list(self.by_path.get(rel_path, ()))

    def mbid_candidates(self, mbids: frozenset[str]) -> list[RemoteTrack]:
        found: dict[str, RemoteTrack] = {}
        for mbid in mbids:
            for track in self.by_mbid.get(mbid.casefold(), ()):
                found[track.track_id] = track
        return list(found.values())

    def isrc_candidates(self, isrcs: frozenset[str]) -> list[RemoteTrack]:
        found: dict[str, RemoteTrack] = {}
        for isrc in isrcs:
            for track in self.by_isrc.get(isrc.casefold(), ()):
                found[track.track_id] = track
        return list(found.values())

    def metadata_candidates(self, track: RemoteTrack) -> list[RemoteTrack]:
        return list(self.by_metadata.get(metadata_key(track), ()))

    def suggest(self, track: RemoteTrack, limit: int = 5, cutoff: float = 0.86) -> list[RemoteTrack]:
        """Fuzzy suggestions only. Never used to auto-accept a match."""
        label = fuzzy_label(track)
        if not label:
            return []
        scored: list[tuple[float, RemoteTrack]] = []
        for candidate_label, tracks in self._fuzzy.items():
            ratio = difflib.SequenceMatcher(None, label, candidate_label).ratio()
            if ratio >= cutoff:
                for candidate in tracks:
                    scored.append((ratio, candidate))
        scored.sort(key=lambda item: (-item[0], item[1].track_id))
        out: list[RemoteTrack] = []
        seen: set[str] = set()
        for _, candidate in scored:
            if candidate.track_id in seen:
                continue
            seen.add(candidate.track_id)
            out.append(candidate)
            if len(out) >= limit:
                break
        return out


def build_index(
    tracks: Iterable[RemoteTrack],
    *,
    paginate: Callable[[], Iterable[RemoteTrack]] | None = None,
    source: str = "",
) -> TargetIndex:
    """Build an index from an iterable or a paginated factory."""
    index = TargetIndex()
    index.source = source
    if paginate is not None:
        index.extend(paginate())
    else:
        index.extend(tracks)
    return index
