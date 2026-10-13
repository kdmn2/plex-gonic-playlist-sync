"""Post-write verification helpers."""

from __future__ import annotations

from ..models import RemotePlaylist


def verify_sequence(actual: RemotePlaylist, expected_ids: list[str]) -> tuple[bool, str]:
    """Verify exact order and duplicate occurrences.

    Returns ``(ok, detail)``. Comparison is by target track id, position by
    position, so duplicates and ordering both matter.
    """
    actual_ids = actual.track_ids()
    if actual_ids == expected_ids:
        return True, f"verified {len(actual_ids)} ordered entries"
    if len(actual_ids) != len(expected_ids):
        detail = f"entry count mismatch: expected {len(expected_ids)}, read back {len(actual_ids)}"
    else:
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(actual_ids, expected_ids, strict=False)) if a != b),
            None,
        )
        detail = f"order/content mismatch at position {first_diff}"
    return False, detail
