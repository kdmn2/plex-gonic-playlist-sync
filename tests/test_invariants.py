import pytest

from playlist_sync.errors import make_error
from playlist_sync.invariants import (
    SAFETY_INVARIANTS,
    DryRunGuard,
    InvariantViolation,
    check_baseline_after_verification,
    check_not_empty_from_unavailable,
    check_sequences_preserved,
    check_target_is_mapped,
)


class _Mapping:
    def __init__(self, plex=None, gonic=None):
        self.plex_playlist_id = plex
        self.gonic_playlist_id = gonic


def test_all_invariants_documented():
    assert len(SAFETY_INVARIANTS) == 12
    assert all(v.summary for v in SAFETY_INVARIANTS.values())


def test_is2_never_mutate_unmapped():
    mapping = _Mapping(plex="1", gonic="pl-a")
    check_target_is_mapped(mapping, "gonic", "pl-a", creating=False)  # ok
    with pytest.raises(InvariantViolation):
        check_target_is_mapped(mapping, "gonic", "pl-other", creating=False)
    # creation is allowed even without an id
    check_target_is_mapped(mapping, "gonic", None, creating=True)


def test_is3_order_and_duplicates_preserved():
    check_sequences_preserved(["a", "b", "a"], ["a", "b", "a"])
    with pytest.raises(InvariantViolation):
        check_sequences_preserved(["a", "b", "a"], ["a", "a", "b"])  # reordered
    with pytest.raises(InvariantViolation):
        check_sequences_preserved(["a", "a"], ["a"])  # duplicate dropped


def test_is4_no_baseline_without_verification():
    with pytest.raises(InvariantViolation):
        check_baseline_after_verification(False)
    check_baseline_after_verification(True)


def test_is5_unavailable_is_not_empty():
    check_not_empty_from_unavailable(None)
    with pytest.raises(InvariantViolation):
        check_not_empty_from_unavailable(make_error("CONNECT_TIMEOUT", "down"))
    with pytest.raises(InvariantViolation):
        check_not_empty_from_unavailable(make_error("STORAGE_CORRUPT", "corrupt"))


def test_is12_dry_run_guard_blocks_mutations():
    guard = DryRunGuard()
    with pytest.raises(InvariantViolation):
        guard.mutation("create_playlist")
    assert guard.violations == ["create_playlist"]
