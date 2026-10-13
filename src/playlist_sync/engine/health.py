"""User-visible health/status derivation for the dashboard.

Distinguishes idle, running, waiting-for-backend, blocked, conflict, recovery
required, paused, needs-confirmation, and configuration/storage problems, and
keeps "last attempt" separate from "last verified success". A skipped or blocked
run is never shown as a successful synchronization.
"""

from __future__ import annotations

from enum import Enum

from ..store import ACTIVE_OPERATION_STATES


class MappingState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_BACKEND = "waiting_backend"
    BLOCKED_MATCHING = "blocked_matching"
    CONFLICT = "conflict"
    RECOVERY_REQUIRED = "recovery_required"
    NEEDS_CONFIRMATION = "needs_confirmation"
    PAUSED = "paused"
    CONFIG_PROBLEM = "config_problem"
    STORAGE_PROBLEM = "storage_problem"
    ERROR = "error"


LABELS = {
    MappingState.IDLE: "Idle / in sync",
    MappingState.RUNNING: "Running",
    MappingState.WAITING_BACKEND: "Waiting for backend",
    MappingState.BLOCKED_MATCHING: "Blocked by unmatched tracks",
    MappingState.CONFLICT: "Conflict — needs review",
    MappingState.RECOVERY_REQUIRED: "Recovery required",
    MappingState.NEEDS_CONFIRMATION: "Needs confirmation",
    MappingState.PAUSED: "Paused",
    MappingState.CONFIG_PROBLEM: "Configuration problem",
    MappingState.STORAGE_PROBLEM: "Storage problem",
    MappingState.ERROR: "Error",
}

CONNECTIVITY_CODES = {
    "CONNECT_DNS",
    "CONNECT_REFUSED",
    "CONNECT_TIMEOUT",
    "CONNECT_TLS",
    "CONNECT_HTTP",
    "BACKEND_UNAVAILABLE",
    "HTTP_RATE_LIMIT",
    "HTTP_SERVER_ERROR",
    "HTTP_MAINTENANCE",
    "RESPONSE_MALFORMED",
    "RESPONSE_NOT_API",
    "MUTATION_OUTCOME_UNKNOWN",
}
MATCH_CODES = {"MATCH_UNMATCHED", "MATCH_AMBIGUOUS"}


def _run_state(run: dict | None) -> MappingState:
    if not run:
        return MappingState.IDLE
    status = run.get("status")
    code = run.get("error_code") or ""
    if status == "running":
        return MappingState.RUNNING
    if status in {"applied_verified", "no_change"}:
        return MappingState.IDLE
    if status == "conflict":
        return MappingState.CONFLICT
    if status in {"outcome_unknown", "partial", "verification_failed", "recovery_required", "failed"}:
        return MappingState.RECOVERY_REQUIRED if status != "failed" else MappingState.ERROR
    if status == "storage_problem":
        return MappingState.STORAGE_PROBLEM
    if code in MATCH_CODES:
        return MappingState.BLOCKED_MATCHING
    if code == "CONFIRMATION_REQUIRED" or code == "CONFIRMATION_STALE":
        return MappingState.NEEDS_CONFIRMATION
    if code in CONNECTIVITY_CODES:
        return MappingState.WAITING_BACKEND
    if status in {"busy"}:
        return MappingState.RUNNING
    if status == "aborted":
        return MappingState.ERROR
    if status == "blocked":
        return MappingState.BLOCKED_MATCHING
    if run.get("error"):
        return MappingState.ERROR
    return MappingState.IDLE


def mapping_health(ctx, mapping) -> dict:
    runs = [r for r in ctx.store.list_runs(limit=200) if r["mapping_id"] == mapping.id] if ctx.has_store else []
    latest = runs[0] if runs else None
    last_success = next(
        (r for r in runs if r.get("verified") or r.get("status") in {"applied_verified", "no_change"}),
        None,
    )
    active = (
        ctx.store.list_operations(mapping_id=mapping.id, states=ACTIVE_OPERATION_STATES, limit=20)
        if ctx.has_store
        else []
    )

    if ctx.state_error is not None:
        state = MappingState.STORAGE_PROBLEM if ctx.state_error.category.value == "storage" else MappingState.RECOVERY_REQUIRED
        detail = ctx.state_error.message
    elif not mapping.enabled or mapping.paused:
        state = MappingState.PAUSED
        detail = "Disabled or paused."
    elif active:
        state = MappingState.RECOVERY_REQUIRED
        detail = f"{len(active)} unfinished operation(s) need reconciliation."
    else:
        state = _run_state(latest)
        detail = (latest or {}).get("error") or ""

    return {
        "id": mapping.id,
        "name": mapping.name,
        "direction": mapping.direction,
        "enabled": mapping.enabled,
        "paused": mapping.paused,
        "state": state.value,
        "label": LABELS[state],
        "detail": detail,
        "last_attempt": {
            "status": (latest or {}).get("status"),
            "when": (latest or {}).get("started_at"),
            "mode": (latest or {}).get("mode"),
            "trigger": (latest or {}).get("trigger"),
            "error_code": (latest or {}).get("error_code"),
        }
        if latest
        else None,
        "last_success": {
            "status": last_success.get("status"),
            "when": last_success.get("finished_at") or last_success.get("started_at"),
        }
        if last_success
        else None,
        "active_operations": [op["operation_key"] for op in active],
    }


def overview(ctx) -> dict:
    storage = ctx.storage_health()
    mappings = [mapping_health(ctx, m) for m in ctx.store.list_mappings()] if ctx.has_store else []
    active_ops = ctx.store.list_active_operations() if ctx.has_store else []
    conflicts = ctx.store.list_conflicts() if ctx.has_store else []
    global_state = MappingState.IDLE
    if not ctx.has_store:
        global_state = MappingState.STORAGE_PROBLEM
    elif ctx.state_error is not None:
        global_state = MappingState.STORAGE_PROBLEM if ctx.state_error.category.value == "storage" else MappingState.RECOVERY_REQUIRED
    elif any(m["state"] == MappingState.RECOVERY_REQUIRED.value for m in mappings):
        global_state = MappingState.RECOVERY_REQUIRED
    elif conflicts:
        global_state = MappingState.CONFLICT
    return {
        "state": global_state.value,
        "label": LABELS[global_state],
        "storage": storage,
        "storage_error": ctx.state_error.to_dict(include_details=True) if ctx.state_error else None,
        "mappings": mappings,
        "active_operations": active_ops,
        "unresolved_conflicts": conflicts,
        "recovery_required": any(
            m["state"] in {MappingState.RECOVERY_REQUIRED.value} for m in mappings
        ) or bool(active_ops),
    }
