"""Confirmation tokens for destructive operations.

A confirmation is bound to the *exact* plan the user reviewed: the token is an
HMAC over the mapping id and a fingerprint of the plan (source/target
fingerprints, desired target ids, rename). If the plan changes, the token no
longer verifies and the UI/CLI must show a fresh preview. Server-side checks are
mandatory; disabling a button in the UI is not sufficient.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from ..errors import make_error
from .planner import Plan


def plan_fingerprint(plan: Plan) -> str:
    target_before = plan.target_playlist.fingerprint() if plan.target_playlist else "none"
    source_fp = plan.source_playlist.fingerprint() if plan.source_playlist else "none"
    parts = [
        plan.mapping.id,
        plan.direction.value,
        source_fp,
        target_before,
        plan.rename_to or "",
        "create" if plan.create_target else "update",
        "\x00".join(plan.desired_ids),
    ]
    return hashlib.sha256("\x01".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfirmationReason:
    code: str
    message: str


def confirmation_reasons(plan: Plan, *, large_reduction_ratio: float = 0.25) -> list[ConfirmationReason]:
    reasons: list[ConfirmationReason] = []
    current = len(plan.current_ids)
    removals = len(plan.diff.removals)

    if plan.target_exists and current > 0 and not plan.desired_ids:
        reasons.append(
            ConfirmationReason(
                "clear_populated",
                f"this clears all {current} tracks from the target playlist",
            )
        )
    threshold = max(5, int(current * large_reduction_ratio))
    if removals > threshold and current:
        reasons.append(
            ConfirmationReason(
                "large_reduction",
                f"this removes {removals} of {current} tracks from the target playlist",
            )
        )
    if plan.unmatched and plan.apply_allowed:
        reasons.append(
            ConfirmationReason(
                "partial",
                f"partial sync will skip {len(plan.unmatched)} unmatched/ambiguous track(s)",
            )
        )
    return reasons


def _secret_bytes(secret: str | None) -> bytes:
    return (secret or "playlist-sync").encode("utf-8")


def issue_token(secret: str | None, mapping_id: str, fingerprint: str) -> str:
    message = f"{mapping_id}:{fingerprint}".encode()
    return hmac.new(_secret_bytes(secret), message, hashlib.sha256).hexdigest()[:32]


def verify_token(secret: str | None, mapping_id: str, fingerprint: str, token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(issue_token(secret, mapping_id, fingerprint), token)


def require_confirmation(
    plan: Plan, *, token: str | None, fingerprint: str, force: bool, secret: str | None
) -> None:
    """Raise a typed error if this plan needs confirmation and it is missing/stale."""
    reasons = confirmation_reasons(plan)
    if not reasons or force:
        return
    if verify_token(secret, plan.mapping.id, fingerprint, token):
        return
    if token:
        raise make_error(
            "CONFIRMATION_STALE",
            "The plan changed since it was reviewed; review the new preview and confirm again.",
        )
    err = make_error(
        "CONFIRMATION_REQUIRED",
        "This change is destructive and needs explicit confirmation: "
        + "; ".join(r.message for r in reasons),
    )
    err.details["confirmation_token"] = issue_token(secret, plan.mapping.id, fingerprint)
    err.details["reasons"] = [r.code for r in reasons]
    raise err
