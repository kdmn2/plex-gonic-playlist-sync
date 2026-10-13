"""Persistent, change-aware caching for library matching indexes.

The expensive artifact is the *library matching index* (one per server): it is
built by reading every library track and normalising metadata/paths/identities.
This module owns:

* deterministic content fingerprints of a library generation (the fields the
  matcher actually reads);
* compact serialisation of the parsed index so a completed generation survives a
  restart;
* the scope / algorithm / configuration keys that decide whether a persisted
  generation is compatible.

Design notes (honest about backend limits):

* A playlist can keep the same entry count while tracks are replaced, reordered,
  duplicated differently, or renamed. A library can keep the same track count
  while paths/tags/identities change. **Counts are never used as proof.** The
  fingerprint below therefore covers ordered identities and every field the
  matcher uses.
* When a backend exposes a trustworthy scan token we can skip the content read
  entirely. When it does not, freshness can only be established by *fetching*
  the current data and comparing fingerprints -- avoiding a rebuild does **not**
  necessarily avoid fetching. That distinction is surfaced in the UI/diagnostics.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from ..matching.normalize import artist_key, normalize_rel_path, normalize_text
from ..models import RemoteTrack, ServerKind

# Bump when index semantics or serialisation change so incompatible persisted
# generations are ignored (and rebuilt) rather than misused.
INDEX_ALGORITHM_VERSION = 1

# RemoteTrack fields we persist. This is exactly the set used by the matcher
# (identities, paths, normalised metadata) plus what presentation needs.
_TRACK_FIELDS = (
    "track_id", "title", "artist", "album_artist", "album",
    "disc_number", "track_number", "year", "duration_ms",
    "path", "rel_path",
)


def library_scope(settings: Any, side: ServerKind) -> str:
    """Scope a library index to the configured set of libraries.

    Different ``plex.library_sections`` (or a different music folder) select a
    different subset of the server, so their indexes are not interchangeable.
    """
    if side == ServerKind.PLEX:
        sections = getattr(getattr(settings, "plex", None), "library_sections", None) or []
        return "sections:" + ",".join(sorted(str(s) for s in sections))
    folder = getattr(getattr(settings, "gonic", None), "music_folder_id", None)
    return f"folder:{folder or ''}"


def matching_config_revision(settings: Any) -> str:
    """Revision of configuration that changes index *contents*.

    Only index-relevant settings belong here: a change to matching tolerances or
    path roots can change what the index stores, whereas unrelated UI settings
    must not invalidate a good index.
    """
    sync = getattr(settings, "sync", None)
    roots = []
    for root in getattr(sync, "path_roots", []) or []:
        roots.append([getattr(root, "name", None), getattr(root, "plex_root", None), getattr(root, "gonic_root", None)])
    payload = {
        "algorithm": INDEX_ALGORITHM_VERSION,
        "duration_tolerance_ms": getattr(sync, "duration_tolerance_ms", None),
        "path_roots": roots,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def _track_token(track: RemoteTrack) -> str:
    """Canonical, deterministic token for the fields the matcher reads."""
    parts = [
        str(track.track_id or ""),
        normalize_rel_path(track.rel_path),
        "|".join(sorted(v.casefold() for v in track.musicbrainz_ids)),
        "|".join(sorted(v.casefold() for v in track.isrcs)),
        artist_key(track.artists, track.album_artist or track.artist),
        normalize_text(track.title),
        normalize_text(track.album),
        str(int(track.disc_number or 0)),
        str(int(track.track_number or 0)),
        str(int(track.duration_ms or 0)),
    ]
    return "\x1f".join(parts)


def fingerprint_tracks(tracks: Iterable[RemoteTrack]) -> str:
    """Content fingerprint of a library generation.

    Library order is irrelevant to matching, so per-track tokens are sorted
    before hashing: a pure reordering does not invalidate the index, but a
    replaced track, a changed path/tag/identity, or an added/removed track does.
    """
    tokens = sorted(_track_token(track) for track in tracks)
    digest = hashlib.sha256()
    for token in tokens:
        digest.update(token.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def serialize_tracks(tracks: Iterable[RemoteTrack]) -> str:
    rows: list[dict[str, Any]] = []
    for track in tracks:
        row: dict[str, Any] = {field: getattr(track, field, None) for field in _TRACK_FIELDS}
        row["server"] = getattr(track.server, "value", track.server)
        row["artists"] = list(track.artists)
        row["musicbrainz_ids"] = sorted(track.musicbrainz_ids)
        row["isrcs"] = sorted(track.isrcs)
        rows.append(row)
    return json.dumps(rows, separators=(",", ":"), ensure_ascii=False)


def deserialize_tracks(payload: str) -> list[RemoteTrack]:
    rows = json.loads(payload)
    tracks: list[RemoteTrack] = []
    for row in rows:
        tracks.append(
            RemoteTrack(
                server=ServerKind(row.get("server") or "gonic"),
                track_id=str(row.get("track_id") or ""),
                title=row.get("title") or "",
                artist=row.get("artist") or "",
                album_artist=row.get("album_artist") or "",
                artists=tuple(row.get("artists") or ()),
                album=row.get("album") or "",
                disc_number=row.get("disc_number"),
                track_number=row.get("track_number"),
                year=row.get("year"),
                duration_ms=row.get("duration_ms"),
                path=row.get("path"),
                rel_path=row.get("rel_path"),
                musicbrainz_ids=frozenset(row.get("musicbrainz_ids") or ()),
                isrcs=frozenset(row.get("isrcs") or ()),
            )
        )
    return tracks


__all__ = [
    "INDEX_ALGORITHM_VERSION",
    "deserialize_tracks",
    "fingerprint_tracks",
    "library_scope",
    "matching_config_revision",
    "serialize_tracks",
]
