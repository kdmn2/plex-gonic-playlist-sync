"""Anchored path-root transformations between Plex and Gonic.

We never do arbitrary string replacement. A configured mapping pairs a Plex
absolute root (the path prefix Plex sees) with a Gonic root (the prefix Gonic
sees). Matching then compares *relative* paths beneath those roots.

Plex exposes absolute file paths. Gonic exposes paths that are already relative
to its configured music folder. Therefore:

- Plex -> relative : strip one configured Plex root at a path-segment boundary.
- Gonic -> relative: normalise the relative path directly; if Gonic ever
  returns an absolute path, strip a configured Gonic root at a boundary.

Multiple roots are supported. Ambiguity (a Plex file under two configured roots)
is reported rather than silently resolved.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..matching.normalize import normalize_rel_path


@dataclass(frozen=True)
class PathRoot:
    name: str
    plex_root: str
    gonic_root: str


def _strip_root(root: str, path: str) -> str | None:
    """Strip ``root`` from ``path`` only at a path-segment boundary."""
    if not root or not path:
        return None
    normal_root = root.replace("\\", "/").rstrip("/")
    normal_path = path.replace("\\", "/")
    if normal_path == normal_root:
        return ""
    prefix = normal_root + "/"
    if normal_path.startswith(prefix):
        return normal_path[len(prefix):]
    return None


class PathMapper:
    def __init__(self, roots: list[PathRoot]):
        self.roots = [r for r in roots if r.plex_root or r.gonic_root]

    @property
    def configured(self) -> bool:
        return bool(self.roots)

    def strip_plex(self, absolute_path: str | None) -> list[str]:
        """Return candidate relative paths for a Plex absolute path.

        Multiple values indicate the path sits under more than one configured
        Plex root; callers must treat that as ambiguous.
        """
        if not absolute_path:
            return []
        found: list[str] = []
        for root in self.roots:
            rel = _strip_root(root.plex_root, absolute_path)
            if rel is not None:
                normalised = normalize_rel_path(rel)
                if normalised and normalised not in found:
                    found.append(normalised)
        return found

    def gonic_rel(self, path: str | None) -> str | None:
        """Return the relative path for a Gonic path (relative or absolute)."""
        if not path:
            return None
        if path.startswith("/"):
            candidates: list[str] = []
            for root in self.roots:
                rel = _strip_root(root.gonic_root, path)
                if rel is not None:
                    norm = normalize_rel_path(rel)
                    if norm and norm not in candidates:
                        candidates.append(norm)
            if len(candidates) == 1:
                return candidates[0]
            return None
        norm = normalize_rel_path(path)
        return norm or None

    def plex_absolute_candidates(self, rel_path: str) -> list[str]:
        """Inverse: plausible Plex absolute paths for a relative path."""
        rel = normalize_rel_path(rel_path)
        if not rel:
            return []
        out: list[str] = []
        for root in self.roots:
            base = root.plex_root.replace("\\", "/").rstrip("/")
            if base:
                candidate = f"{base}/{rel}"
                if candidate not in out:
                    out.append(candidate)
        return out
