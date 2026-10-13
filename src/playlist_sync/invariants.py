"""Explicit safety invariants, enforced by the engine and asserted by tests.

These are the rules that protect existing playlists and state. They are small,
pure functions so both runtime code and tests can call them; the docstrings and
:data:`SAFETY_INVARIANTS` are the human-readable specification.

A violation raises :class:`InvariantViolation` (an :class:`~playlist_sync.errors.AppError`).
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import AppError, ErrorCategory, ErrorScope, Outcome, Retryability


class InvariantViolation(AppError):
    def __init__(self, invariant_id: str, message: str):
        super().__init__(
            code="INTERNAL",
            message=f"safety invariant {invariant_id} violated: {message}",
            category=ErrorCategory.INTERNAL,
            scope=ErrorScope.APPLICATION,
            retryability=Retryability.NEVER,
            outcome=Outcome.FAILED_SAFE,
        )
        self.invariant_id = invariant_id


@dataclass(frozen=True)
class Invariant:
    id: str
    summary: str


SAFETY_INVARIANTS: dict[str, Invariant] = {
    "IS-1": Invariant("IS-1", "Never delete or modify audio files."),
    "IS-2": Invariant("IS-2", "Never mutate an unmapped playlist."),
    "IS-3": Invariant("IS-3", "Never silently discard duplicate entries or reorder tracks."),
    "IS-4": Invariant("IS-4", "Never advance a successful-sync baseline after an unverified write."),
    "IS-5": Invariant("IS-5", "Never interpret an unavailable server or incomplete response as an empty/deleted playlist."),
    "IS-6": Invariant("IS-6", "Never overwrite a playlist that changed after the plan was generated."),
    "IS-7": Invariant("IS-7", "Never silently reset a missing or corrupted state database."),
    "IS-8": Invariant("IS-8", "Never retry an uncertain remote mutation without first reconciling state."),
    "IS-9": Invariant("IS-9", "Never expose secrets in logs, API responses, UI errors, or diagnostics."),
    "IS-10": Invariant("IS-10", "A failed playlist operation must not corrupt other mappings."),
    "IS-11": Invariant("IS-11", "One authoritative executor controls mutations for a shared state directory."),
    "IS-12": Invariant("IS-12", "A dry run causes no remote mutations."),
}


def check_target_is_mapped(mapping, side: str, playlist_id: str | None, *, creating: bool) -> None:
    """IS-2: the target playlist must be the mapping's configured target."""
    if creating:
        return
    configured = (
        mapping.gonic_playlist_id if side == "gonic" else mapping.plex_playlist_id
    )
    if not configured or configured != playlist_id:
        raise InvariantViolation(
            "IS-2",
            f"refusing to mutate playlist {playlist_id!r} on {side} which is not the mapping target",
        )


def check_sequences_preserved(expected_ids: list[str], actual_ids: list[str]) -> None:
    """IS-3: order and duplicate occurrences must be preserved exactly."""
    if expected_ids != actual_ids:
        raise InvariantViolation(
            "IS-3",
            f"expected {len(expected_ids)} ordered entries, observed {len(actual_ids)}",
        )


def check_baseline_after_verification(verified: bool) -> None:
    """IS-4: a baseline may only advance once a write is verified."""
    if not verified:
        raise InvariantViolation("IS-4", "refusing to advance the baseline without verification")


def check_not_empty_from_unavailable(error: AppError | None) -> None:
    """IS-5: a connectivity/state error must never be turned into an empty playlist."""
    if error is None:
        return
    if error.category in {ErrorCategory.CONNECTIVITY, ErrorCategory.STATE_INTEGRITY}:
        raise InvariantViolation(
            "IS-5",
            f"refusing to treat {error.code} as an empty playlist",
        )


class DryRunGuard:
    """IS-12: any attempt to mutate a backend during a dry run is a bug."""

    def __init__(self) -> None:
        self._violations: list[str] = []

    def mutation(self, what: str) -> None:
        self._violations.append(what)
        raise InvariantViolation("IS-12", f"mutation attempted during dry run: {what}")

    @property
    def violations(self) -> list[str]:
        return list(self._violations)
