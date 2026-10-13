"""Conservative text and path normalisation.

Rules of thumb:
- Unicode is normalised with NFC.
- Whitespace is collapsed; surrounding whitespace stripped.
- Case is folded only for *comparison keys*, never for stored values.
- We do **not** strip punctuation, accents, or bracketed qualifiers, because
  that merges genuinely different editions (live/remix/remaster/clean/explicit).
- Multi-valued artists are split only on explicit separators, never on ``&``.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
# Only these are treated as multi-value separators. Notably excluded: "&",
# ",", "/", "+", "x" — all of which occur inside a single artist's name.
_ARTIST_SEPARATORS = re.compile(r"\s*;\s*")
# Known disambiguating qualifiers we keep (and can surface) rather than erase.
VERSION_QUALIFIERS = (
    "live",
    "studio",
    "remaster",
    "remastered",
    "remix",
    "cover",
    "clean",
    "explicit",
    "instrumental",
    "acoustic",
    "demo",
    "edit",
    "mono",
    "stereo",
    "deluxe",
)


def nfc(value: str | None) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", value)


def collapse_ws(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def normalize_text(value: str | None) -> str:
    """Normalised, case-folded comparison key for free-text metadata."""
    return collapse_ws(nfc(value)).casefold()


def normalize_display(value: str | None) -> str:
    """Whitespace/NFC-normalised value suitable for display and storage."""
    return collapse_ws(nfc(value))


def split_artists(value: str | None) -> tuple[str, ...]:
    """Split a multi-valued artist field conservatively.

    Splits on ``;`` only. ``&`` and ``,`` are kept because they are frequently
    part of a single artist name ("Earth, Wind & Fire", "Simon & Garfunkel").
    Empty parts are dropped; the result preserves source order.
    """
    if not value:
        return ()
    parts = [normalize_display(p) for p in _ARTIST_SEPARATORS.split(nfc(value))]
    return tuple(p for p in parts if p)


def artist_key(artists: tuple[str, ...], fallback: str | None) -> str:
    """Canonical key for a set of artists, order-insensitive."""
    values = artists or split_artists(fallback)
    if not values:
        values = (fallback,) if fallback else ()
    keys = sorted(normalize_text(v) for v in values if v)
    return " ; ".join(keys)


def normalize_rel_path(value: str | None) -> str:
    """Normalise a *relative* path: POSIX separators, NFC, no leading ``./``.

    Case is preserved (Linux is case-sensitive). ``.`` and ``..`` segments are
    resolved lexically so that ``a/./b`` and ``a/b`` compare equal, while a
    leading ``..`` is rejected (returns ``""``) because it escapes the root.
    """
    if not value:
        return ""
    raw = nfc(value).replace("\\", "/")
    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                return ""
            parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


def has_qualifier(*values: str | None) -> str | None:
    """Return the first edition qualifier found, if any (for candidate detail)."""
    joined = " ".join(normalize_text(v) for v in values if v)
    for qualifier in VERSION_QUALIFIERS:
        if re.search(rf"\b{re.escape(qualifier)}\b", joined):
            return qualifier
    return None
