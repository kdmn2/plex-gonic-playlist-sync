"""Planning: turn a mapping + live playlists into a dry-run plan.

Nothing here mutates a backend. The planner reads, matches, applies policy, and
produces a :class:`Plan` that :mod:`playlist_sync.engine.apply` can execute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..backends import PlaylistNotFound
from ..matching.matcher import TrackMatcher
from ..models import (
    BaselineChoice,
    Diff,
    Direction,
    MatchResult,
    PlannedEntry,
    RemotePlaylist,
    ServerKind,
)
from ..store.db import Mapping
from .context import EngineContext
from .diff import ordered_diff, sequence_equal

log = logging.getLogger("playlist_sync.engine.planner")


@dataclass
class Plan:
    mapping: Mapping
    direction: Direction
    source_side: ServerKind
    target_side: ServerKind
    source_playlist: RemotePlaylist | None = None
    target_playlist: RemotePlaylist | None = None
    desired_ids: list[str] = field(default_factory=list)
    current_ids: list[str] = field(default_factory=list)
    diff: Diff = field(default_factory=Diff)
    matched: list[PlannedEntry] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    blocked: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rename_to: str | None = None
    create_target: bool = False
    target_exists: bool = True
    no_change: bool = False
    conflict: bool = False
    write_side: ServerKind | None = None
    bootstrap: bool = False

    @property
    def apply_allowed(self) -> bool:
        return not self.blocked and not self.conflict and not self.no_change

    def summary(self) -> dict:
        return {
            "mapping": self.mapping.name,
            "direction": self.direction.value,
            "source": self.source_side.value,
            "target": self.target_side.value,
            "no_change": self.no_change,
            "blocked": self.blocked,
            "conflict": self.conflict,
            "create_target": self.create_target,
            "rename_to": self.rename_to,
            "additions": len(self.diff.additions),
            "removals": len(self.diff.removals),
            "moves": len(self.diff.moves),
            "order_changed": self.diff.order_changed,
            "unmatched": len(self.unmatched),
            "reasons": list(self.blocked_reasons),
        }


def _source_playlist_id(mapping: Mapping, side: ServerKind) -> str | None:
    return mapping.plex_playlist_id if side == ServerKind.PLEX else mapping.gonic_playlist_id


def _target_playlist_id(mapping: Mapping, side: ServerKind) -> str | None:
    return mapping.plex_playlist_id if side == ServerKind.PLEX else mapping.gonic_playlist_id


def _get_playlist(ctx: EngineContext, side: ServerKind, playlist_id: str | None) -> RemotePlaylist | None:
    if not playlist_id:
        return None
    try:
        return ctx.backend(side).get_playlist(playlist_id)
    except PlaylistNotFound:
        return None


def _label_map(target_pl: RemotePlaylist | None, matched: list[PlannedEntry]) -> dict[str, str]:
    labels: dict[str, str] = {}
    if target_pl:
        for entry in target_pl.entries:
            labels[entry.track.track_id] = entry.track.identity_hint()
    for planned in matched:
        if planned.target_track:
            labels[planned.target_track.track_id] = planned.target_track.identity_hint()
    return labels


def plan_mapping(ctx: EngineContext, mapping: Mapping, *, trigger: str = "manual") -> Plan:
    direction = Direction(mapping.direction)
    if direction == Direction.BIDIRECTIONAL:
        return plan_bidirectional(ctx, mapping)
    if direction == Direction.PLEX_TO_GONIC:
        source_side, target_side = ServerKind.PLEX, ServerKind.GONIC
    else:
        source_side, target_side = ServerKind.GONIC, ServerKind.PLEX
    return _plan_between(ctx, mapping, source_side, target_side, bootstrap=False)


def _plan_between(
    ctx: EngineContext,
    mapping: Mapping,
    source_side: ServerKind,
    target_side: ServerKind,
    *,
    bootstrap: bool,
) -> Plan:
    direction = Direction(mapping.direction)
    plan = Plan(mapping=mapping, direction=direction, source_side=source_side, target_side=target_side)
    plan.bootstrap = bootstrap

    source_id = _source_playlist_id(mapping, source_side)
    target_id = _target_playlist_id(mapping, target_side)
    plan.write_side = target_side

    if not source_id:
        plan.blocked = True
        plan.blocked_reasons.append(f"no {source_side.value} playlist is configured as the source")
        return plan

    source_pl = _get_playlist(ctx, source_side, source_id)
    if source_pl is None:
        # A mapped playlist that disappeared: pause and report (never guess).
        ctx.store.update_mapping(mapping.id, paused=True)
        plan.blocked = True
        plan.blocked_reasons.append(
            f"mapped source playlist {source_id!r} on {source_side.value} no longer exists; "
            "mapping paused"
        )
        return plan
    plan.source_playlist = source_pl

    target_pl = _get_playlist(ctx, target_side, target_id)
    plan.target_playlist = target_pl
    plan.target_exists = target_pl is not None

    # -- prepare matching ------------------------------------------------------
    target_index = ctx.library_index(target_side)
    overrides = ctx.store.overrides_for(mapping.id, source_side.value)
    matcher = TrackMatcher(
        target_index,
        overrides=overrides,
        duration_tolerance_ms=ctx.settings.sync.duration_tolerance_ms,
    )

    desired: list[str] = []
    matched: list[PlannedEntry] = []
    for entry in source_pl.entries:
        result: MatchResult = matcher.match(entry.track)
        if result.is_confident and result.match is not None:
            desired.append(result.match.track_id)
            matched.append(
                PlannedEntry(
                    source_index=entry.index,
                    source_track=entry.track,
                    target_track=result.match,
                    match=result,
                )
            )
        else:
            plan.unmatched.append(
                {
                    "index": entry.index,
                    "track_id": entry.track.track_id,
                    "label": entry.track.identity_hint(),
                    "status": result.status,
                    "method": result.method,
                    "detail": result.detail,
                    "suggestions": [
                        {"track_id": c.track.track_id, "label": c.track.identity_hint()}
                        for c in (result.candidates or matcher.suggestions(entry.track))
                    ],
                }
            )
    plan.matched = matched
    plan.desired_ids = desired

    # -- policies --------------------------------------------------------------
    allow_partial = bool(mapping.allow_partial or ctx.settings.sync.allow_partial)
    if plan.unmatched:
        if ctx.settings.sync.block_on_unmatched and not allow_partial:
            plan.blocked = True
            plan.blocked_reasons.append(
                f"{len(plan.unmatched)} source track(s) are unmatched or ambiguous; "
                "playlist write blocked (review and add explicit mappings, or opt into partial sync)"
            )
        else:
            plan.notes.append(
                f"partial sync: {len(plan.unmatched)} unmatched/ambiguous source track(s) will be skipped"
            )

    if any(m.source_track.extra.get("ambiguous_path_roots") for m in matched):
        plan.notes.append(
            "some source tracks sit under multiple configured path roots; their relative path is ambiguous"
        )
    if target_pl is not None and target_pl.summary.extra.get("read_warning"):
        plan.notes.append(f"target read warning: {target_pl.summary.extra['read_warning']}")

    if (
        target_side == ServerKind.PLEX
        and plan.target_exists
        and target_pl is not None
        and target_pl.summary.is_smart
    ):
        plan.blocked = True
        plan.blocked_reasons.append("the Plex target is a smart playlist (read-only); refusing to write")

    empty_source = len(source_pl.entries) == 0
    if (
        empty_source
        and plan.target_exists
        and target_pl is not None
        and target_pl.entries
        and not ctx.settings.sync.allow_clear_target_when_source_empty
    ):
        plan.blocked = True
        plan.blocked_reasons.append(
            "source playlist is empty and the target is populated; clearing requires explicit "
            "sync.allow_clear_target_when_source_empty=true"
        )

    # -- diff / creation -------------------------------------------------------
    if plan.target_exists and target_pl is not None:
        plan.current_ids = target_pl.track_ids()
        labels = _label_map(target_pl, matched)
        plan.diff = ordered_diff(plan.current_ids, plan.desired_ids, label=lambda t: labels.get(t, t))
    else:
        plan.create_target = True
        plan.current_ids = []
        labels = _label_map(None, matched)
        plan.diff = ordered_diff([], plan.desired_ids, label=lambda t: labels.get(t, t))
        if not plan.desired_ids and not ctx.backend(target_side).supports_empty_create():
            plan.blocked = True
            plan.blocked_reasons.append(
                f"{target_side.value} cannot create an empty playlist via the API; "
                "add at least one matchable track first"
            )

    # -- rename ----------------------------------------------------------------
    if target_pl is not None and source_pl.summary.name and target_pl.summary.name != source_pl.summary.name:
        plan.rename_to = source_pl.summary.name

    plan.no_change = (
        not plan.blocked
        and not plan.conflict
        and not plan.create_target
        and not plan.rename_to
        and sequence_equal(plan.current_ids, plan.desired_ids)
    )
    if plan.no_change:
        plan.notes.append("target already matches the source; no writes required")
    return plan


# --------------------------------------------------------------------------- #
# Bidirectional
# --------------------------------------------------------------------------- #


def _canonical_in_plex_space(
    ctx: EngineContext, plex_pl: RemotePlaylist, gonic_pl: RemotePlaylist
):
    """Project both playlists into Plex track-id space for comparison."""
    plex_index = ctx.library_index(ServerKind.PLEX)
    matcher = TrackMatcher(plex_index, duration_tolerance_ms=ctx.settings.sync.duration_tolerance_ms)
    canon_plex = [entry.track.track_id for entry in plex_pl.entries]
    canon_gonic: list[str | None] = []
    for entry in gonic_pl.entries:
        result = matcher.match(entry.track)
        canon_gonic.append(result.match.track_id if result.is_confident and result.match else None)
    return canon_plex, canon_gonic


def plan_bidirectional(ctx: EngineContext, mapping: Mapping) -> Plan:
    plan = Plan(
        mapping=mapping,
        direction=Direction.BIDIRECTIONAL,
        source_side=ServerKind.PLEX,
        target_side=ServerKind.GONIC,
    )
    plex_pl = _get_playlist(ctx, ServerKind.PLEX, mapping.plex_playlist_id)
    gonic_pl = _get_playlist(ctx, ServerKind.GONIC, mapping.gonic_playlist_id)

    if plex_pl is None and gonic_pl is None:
        plan.blocked = True
        plan.blocked_reasons.append("neither side of the mapping exists")
        return plan

    plex_smart = plex_pl is not None and plex_pl.summary.is_smart
    if plex_smart:
        plan.blocked = True
        plan.blocked_reasons.append(
            "the Plex playlist is a smart playlist; bidirectional sync is not allowed for smart playlists"
        )
        return plan

    baseline = ctx.store.get_baseline(mapping.id)
    ready = bool(mapping.baseline_ready and baseline)

    if not ready:
        return _bootstrap(ctx, mapping, plan, plex_pl, gonic_pl)

    base_plex = _baseline_ids(baseline, "plex_entries")
    base_gonic = _baseline_ids(baseline, "gonic_entries")
    plex_ids = plex_pl.track_ids() if plex_pl else []
    gonic_ids = gonic_pl.track_ids() if gonic_pl else []

    plex_changed = plex_ids != base_plex
    gonic_changed = gonic_ids != base_gonic

    if not plex_changed and not gonic_changed:
        plan.no_change = True
        plan.notes.append("neither side changed since the last successful sync")
        return plan

    if plex_changed and not gonic_changed:
        return _plan_between(ctx, mapping, ServerKind.PLEX, ServerKind.GONIC, bootstrap=False)
    if gonic_changed and not plex_changed:
        return _plan_between(ctx, mapping, ServerKind.GONIC, ServerKind.PLEX, bootstrap=False)

    # both changed: compare after canonical matching
    if plex_pl is not None and gonic_pl is not None:
        canon_plex, canon_gonic = _canonical_in_plex_space(ctx, plex_pl, gonic_pl)
        if canon_plex == canon_gonic and all(x is not None for x in canon_gonic):
            plan.no_change = True
            plan.notes.append(
                "both sides changed identically after canonical matching; baseline will be refreshed"
            )
            return plan

    plan.conflict = True
    plan.blocked = True
    plan.blocked_reasons.append(
        "both sides changed differently since the last successful sync; resolve the conflict explicitly"
    )
    return plan


def _bootstrap(
    ctx: EngineContext,
    mapping: Mapping,
    plan: Plan,
    plex_pl: RemotePlaylist | None,
    gonic_pl: RemotePlaylist | None,
) -> Plan:
    mode = BaselineChoice(mapping.baseline_mode)
    if mode == BaselineChoice.MANUAL_REVIEW:
        plan.blocked = True
        plan.blocked_reasons.append(
            "bidirectional baseline is not established; choose 'plex wins', 'gonic wins', or "
            "manually review before syncing"
        )
        return plan
    if mode == BaselineChoice.PLEX_WINS:
        if plex_pl is None:
            plan.blocked = True
            plan.blocked_reasons.append("baseline 'plex wins' selected but the Plex playlist is missing")
            return plan
        return _plan_between(ctx, mapping, ServerKind.PLEX, ServerKind.GONIC, bootstrap=True)
    if gonic_pl is None:
        plan.blocked = True
        plan.blocked_reasons.append("baseline 'gonic wins' selected but the Gonic playlist is missing")
        return plan
    return _plan_between(ctx, mapping, ServerKind.GONIC, ServerKind.PLEX, bootstrap=True)


def _baseline_ids(baseline: dict, key: str) -> list[str]:
    import json

    raw = baseline.get(key)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [e.get("track_id") for e in entries]
