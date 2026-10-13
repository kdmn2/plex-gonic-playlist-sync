"""Human-readable presentation of planned tracks.

The planner and store deliberately speak in stable track ids so matching and
mutations never depend on display strings. This module turns those ids back into
readable rows for the UI, using metadata that is already available on the plan
and in the cached library indexes (never a per-track network request).

Rules (kept deliberately boring so they are easy to test):
1. Prefer ``artist – title``.
2. Fall back to the verified filename basename when title metadata is missing.
3. When neither exists, show an explicit unknown-track label and keep the id in
   technical details -- never present the id as if it were the title.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Playlist attribute names that may carry tracks we can label from, in a
# preferred order (target-side playlists first).
_PLAYLIST_ATTRS = (
    "target_playlist",
    "dest_playlist",
    "playlist_a",
    "playlist_b",
    "source_playlist",
)


def format_duration(duration_ms: Any) -> str:
    if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
        return ""
    seconds = int(duration_ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _text(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def track_row(track_id: str, track: Any) -> dict:
    """One readable row for a track id, given an optional resolved track."""
    if track is None:
        return {
            "id": track_id,
            "primary": "",
            "secondary": "",
            "filename": "",
            "path": "",
            "has_meta": False,
        }
    title = _text(getattr(track, "title", ""))
    artist = _text(getattr(track, "artist", "")) or _text(getattr(track, "album_artist", ""))
    album = _text(getattr(track, "album", ""))
    path = getattr(track, "path", None) or getattr(track, "rel_path", None) or ""
    filename = path.rsplit("/", 1)[-1] if path else ""
    primary = " – ".join(part for part in (artist, title) if part) or filename
    secondary = " · ".join(part for part in (album, format_duration(getattr(track, "duration_ms", None))) if part)
    if not secondary and filename and primary != filename:
        secondary = filename
    return {
        "id": track_id,
        "primary": primary,
        "secondary": secondary,
        "filename": filename,
        "path": path,
        "has_meta": bool(title or artist or album),
    }


def collect_tracks(plan: Any, ctx: Any, ids: Iterable[str]) -> dict:
    """Best-effort ``track_id -> RemoteTrack`` map from plan data and caches."""
    ids = list(ids)
    tracks: dict[str, Any] = {}
    for track_id, track in (getattr(plan, "target_tracks", {}) or {}).items():
        if track is not None:
            tracks.setdefault(track_id, track)
    for planned in getattr(plan, "matched", []) or []:
        target = getattr(planned, "target_track", None)
        if target is not None:
            tracks.setdefault(target.track_id, target)
    for attr in _PLAYLIST_ATTRS:
        playlist = getattr(plan, attr, None)
        if playlist is None:
            continue
        for entry in getattr(playlist, "entries", ()) or ():
            tracks.setdefault(entry.track.track_id, entry.track)
    # Last resort: the already-built, cached destination index (a single
    # in-memory lookup per id, never a network request).
    side = getattr(plan, "target_side", None) or getattr(plan, "dest_side", None)
    if side is not None and ctx is not None and hasattr(ctx, "index_cache"):
        index = ctx.index_cache.get(getattr(side, "value", side))
        if index is not None:
            for track_id in ids:
                if track_id not in tracks:
                    track = index.get(track_id)
                    if track is not None:
                        tracks[track_id] = track
    return tracks


def track_list(plan: Any, ctx: Any, ids: Iterable[str], heading: str, limit: int = 50) -> dict:
    """Ordered, readable rows for a planned list of destination track ids.

    Order and duplicate occurrences are preserved; ``total`` always reflects the
    real planned count even when only ``limit`` rows are shown.
    """
    ids = list(ids or [])
    tracks = collect_tracks(plan, ctx, ids)
    rows = [track_row(track_id, tracks.get(track_id)) for track_id in ids[:limit]]
    return {
        "heading": heading,
        "rows": rows,
        "total": len(ids),
        "shown": min(limit, len(ids)),
    }
