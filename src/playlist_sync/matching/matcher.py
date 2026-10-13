"""Deterministic, explainable source->target track matching.

Priority (from the specification):
  1. user-approved explicit mapping (override)
  2. exact normalised relative file path
  3. compatible stable identifier (MusicBrainz recording id) when exposed
  4. exact normalised metadata + duration tolerance

Invariants:
  - A single candidate is required; ties are reported as ``ambiguous``.
  - Duration is compared with a tolerance; candidates outside it are rejected.
  - Fuzzy suggestions are surfaced but never auto-accepted.
"""

from __future__ import annotations

from ..models import MatchCandidate, MatchResult, RemoteTrack
from .index import TargetIndex, metadata_key
from .normalize import artist_key, normalize_text


class TrackMatcher:
    def __init__(
        self,
        index: TargetIndex,
        *,
        overrides: dict[str, str] | None = None,
        duration_tolerance_ms: int = 2000,
    ):
        self.index = index
        self.overrides = overrides or {}
        self.duration_tolerance_ms = max(0, duration_tolerance_ms)
        self._by_id: dict[str, RemoteTrack] = {t.track_id: t for t in index.tracks}

    # -- individual strategies -------------------------------------------------

    def _override(self, source: RemoteTrack) -> MatchResult | None:
        target_id = self.overrides.get(source.track_id)
        if not target_id:
            return None
        target = self._by_id.get(target_id)
        if target is None:
            return MatchResult(
                match=None,
                method="override",
                status="unmatched",
                detail=f"override target {target_id!r} is not present in the target library",
            )
        return MatchResult(
            match=target,
            method="override",
            status="override",
            detail=f"explicit mapping to {target_id}",
        )

    def _by_path(self, source: RemoteTrack) -> MatchResult | None:
        if not source.rel_path:
            return None
        candidates = self.index.path_candidates(source.rel_path)
        if not candidates:
            return None
        return self._resolve("path", candidates, f"relative path {source.rel_path!r}")

    def _by_mbid(self, source: RemoteTrack) -> MatchResult | None:
        if not source.musicbrainz_ids:
            return None
        candidates = self.index.mbid_candidates(source.musicbrainz_ids)
        if not candidates:
            return None
        return self._resolve("musicbrainz", candidates, "MusicBrainz id")

    def _by_isrc(self, source: RemoteTrack) -> MatchResult | None:
        if not source.isrcs:
            return None
        candidates = self.index.isrc_candidates(source.isrcs)
        if not candidates:
            return None
        return self._resolve("isrc", candidates, "ISRC")

    def _by_metadata(self, source: RemoteTrack) -> MatchResult | None:
        candidates = self.index.metadata_candidates(source)
        if not candidates:
            return None
        candidates = self._apply_duration_filter(source, candidates)
        if not candidates:
            return MatchResult(
                match=None,
                method="metadata",
                status="unmatched",
                detail="metadata matched but duration was outside tolerance",
            )
        return self._resolve("metadata", candidates, "artist/title/album/disc/track" + self._dur_suffix(source))

    # -- helpers ---------------------------------------------------------------

    def _dur_suffix(self, source: RemoteTrack) -> str:
        if source.duration_ms is None:
            return ""
        return f" (duration ±{self.duration_tolerance_ms}ms)"

    def _apply_duration_filter(
        self, source: RemoteTrack, candidates: list[RemoteTrack]
    ) -> list[RemoteTrack]:
        if source.duration_ms is None:
            # No duration to compare against; keep all candidates so a tie stays
            # ambiguous rather than being silently accepted.
            return candidates
        with_duration = [c for c in candidates if c.duration_ms is not None]
        if not with_duration:
            # Target exposes no durations at all; fall back to metadata and let
            # the multiplicity decide (one candidate -> matched, many -> ambiguous).
            return candidates
        return [
            c
            for c in with_duration
            if abs(c.duration_ms - source.duration_ms) <= self.duration_tolerance_ms
        ]

    def _resolve(self, method: str, candidates: list[RemoteTrack], detail: str) -> MatchResult:
        unique: dict[str, RemoteTrack] = {}
        for candidate in candidates:
            unique[candidate.track_id] = candidate
        values = list(unique.values())
        if len(values) == 1:
            return MatchResult(match=values[0], method=method, status="matched", detail=detail)
        return MatchResult(
            match=None,
            method=method,
            status="ambiguous",
            candidates=[MatchCandidate(track=c, method=method, score=1.0, detail=detail) for c in values],
            detail=f"{len(values)} equally plausible candidates by {detail}",
        )

    # -- public ----------------------------------------------------------------

    def match(self, source: RemoteTrack) -> MatchResult:
        override = self._override(source)
        if override is not None:
            return override
        for strategy in (self._by_path, self._by_mbid, self._by_isrc, self._by_metadata):
            result = strategy(source)
            if result is not None and result.status == "ambiguous":
                return result
            if result is not None and result.status == "matched":
                return result
            if result is not None and result.status == "unmatched" and strategy is self._by_metadata:
                # metadata is the last strategy, so a rejection here is final
                return result
        return MatchResult(
            match=None,
            method="none",
            status="unmatched",
            detail=self._unmatched_detail(source),
        )

    def _unmatched_detail(self, source: RemoteTrack) -> str:
        hints = []
        if not source.rel_path:
            hints.append("source exposes no usable relative path")
        if not self.index.size:
            hints.append("target library index is empty (rescan or fix root mapping?)")
        return "; ".join(hints) or "no path, identifier, or metadata candidate found"

    def suggestions(self, source: RemoteTrack, limit: int = 5) -> list[MatchCandidate]:
        key = metadata_key(source)
        artist = artist_key(source.artists, source.album_artist or source.artist)
        if key[1] == "" and artist == "":
            return []
        return [
            MatchCandidate(track=t, method="fuzzy", score=0.0, detail="fuzzy suggestion")
            for t in self.index.suggest(source, limit=limit)
        ]


def indistinguishable(left: RemoteTrack, right: RemoteTrack) -> bool:
    """True if two tracks have identical normalised metadata (edition-agnostic)."""
    return metadata_key(left) == metadata_key(right) and normalize_text(left.title) == normalize_text(
        right.title
    )
