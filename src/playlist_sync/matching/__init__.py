"""Track matching: normalisation, path mapping, indexing, and matching."""

from .index import TargetIndex, build_index, metadata_key
from .matcher import TrackMatcher
from .normalize import normalize_rel_path, normalize_text
from .paths import PathMapper, PathRoot

__all__ = [
    "PathMapper",
    "PathRoot",
    "TargetIndex",
    "TrackMatcher",
    "build_index",
    "metadata_key",
    "normalize_rel_path",
    "normalize_text",
]
