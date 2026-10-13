"""Ordered diff between a current and a desired track-id sequence."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable

from ..models import Diff


def sequence_equal(left: list[str], right: list[str]) -> bool:
    return left == right


def ordered_diff(
    current: list[str],
    desired: list[str],
    *,
    label: Callable[[str], str] | None = None,
) -> Diff:
    """Compute additions/removals/moves as multisets, preserving counts."""
    lab = label or (lambda track_id: track_id)
    current_counts = Counter(current)
    desired_counts = Counter(desired)

    additions: list[str] = []
    removals: list[str] = []
    for track_id, count in desired_counts.items():
        delta = count - current_counts.get(track_id, 0)
        additions.extend([track_id] * max(0, delta))
    for track_id, count in current_counts.items():
        delta = count - desired_counts.get(track_id, 0)
        removals.extend([track_id] * max(0, delta))

    unchanged = sum(min(current_counts.get(t, 0), desired_counts.get(t, 0)) for t in current_counts)

    order_changed = False
    moves: list[str] = []
    if not additions and not removals and current != desired:
        order_changed = True
        # describe which positions move (first few)
        positions: dict[str, list[int]] = defaultdict(list)
        for index, track_id in enumerate(current):
            positions[track_id].append(index)
        for index, track_id in enumerate(desired):
            if positions[track_id]:
                if positions[track_id][0] != index:
                    moves.append(f"{lab(track_id)} → position {index + 1}")
                positions[track_id].pop(0)
            if len(moves) >= 20:
                break

    return Diff(
        additions=[lab(t) for t in additions],
        removals=[lab(t) for t in removals],
        addition_ids=list(additions),
        removal_ids=list(removals),
        moves=moves,
        unchanged=unchanged,
        order_changed=order_changed,
    )
