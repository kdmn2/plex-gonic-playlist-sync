"""Typed, structured error model.

Every failure that crosses a process or API boundary is represented as an
:class:`AppError` carrying a stable machine code, a category and scope, a safe
user-facing message, a suggested next action, a retryability classification, an
outcome classification, and correlation ids. Raw tracebacks are never shown to
users; sanitised technical details are available for diagnostics only.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .redact import scrub


class ErrorCategory(str, Enum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CONNECTIVITY = "connectivity"
    BACKEND_CAPABILITY = "backend_capability"
    MATCHING = "matching"
    CONFLICT = "conflict"
    STORAGE = "storage"
    STATE_INTEGRITY = "state_integrity"
    REMOTE_MUTATION = "remote_mutation"
    CANCELLATION = "cancellation"
    INTERNAL = "internal"


class ErrorScope(str, Enum):
    APPLICATION = "application"
    SERVER = "server"
    MAPPING = "mapping"
    OPERATION = "operation"


class Retryability(str, Enum):
    """Whether/how a failed operation may be retried."""

    NEVER = "never"                      # do not retry (bad config, auth, 4xx)
    TRANSIENT = "transient"              # safe to retry after backoff (reads, connect)
    AFTER_RECONCILE = "after_reconcile"  # re-read remote state before any retry (writes)
    USER_ACTION = "user_action"          # needs the user to change something first


class Outcome(str, Enum):
    """What is known about remote effects."""

    NOT_STARTED = "not_started"                  # failed before any mutation
    FAILED_SAFE = "failed_safe"                  # remote confirmed unchanged
    OUTCOME_UNKNOWN = "outcome_unknown"          # may or may not have applied
    PARTIALLY_APPLIED = "partially_applied"      # some but not all applied
    VERIFICATION_FAILED = "verification_failed"  # applied but did not match intention
    COMPLETED = "completed"                      # completed and verified
    RECOVERY_REQUIRED = "recovery_required"      # cannot classify automatically
    CONFLICT = "conflict"                        # both sides changed differently
    CANCELLED = "cancelled"                      # stopped before/around a mutation
    SKIPPED = "skipped"                          # deliberately not run


@dataclass(frozen=True)
class ErrorSpec:
    category: ErrorCategory
    scope: ErrorScope
    retryability: Retryability = Retryability.NEVER
    outcome: Outcome = Outcome.NOT_STARTED
    default_action: str = ""
    http_status: int = 500


# Stable machine-readable codes. Do not rename existing codes once published;
# add new ones instead.
ERROR_SPECS: dict[str, ErrorSpec] = {
    # configuration
    "CONFIG_INVALID": ErrorSpec(ErrorCategory.CONFIGURATION, ErrorScope.APPLICATION, Retryability.USER_ACTION, default_action="Correct the reported configuration fields and restart.", http_status=400),
    "CONFIG_SECRET_FILE": ErrorSpec(ErrorCategory.CONFIGURATION, ErrorScope.APPLICATION, Retryability.USER_ACTION, default_action="Fix the secret file path/permissions, or set the value directly.", http_status=400),
    "CONFIG_PATH_ROOTS": ErrorSpec(ErrorCategory.CONFIGURATION, ErrorScope.APPLICATION, Retryability.USER_ACTION, default_action="Fix sync.path_roots so each Plex root maps to exactly one Gonic root.", http_status=400),
    # storage / state
    "STORAGE_UNAVAILABLE": ErrorSpec(ErrorCategory.STORAGE, ErrorScope.APPLICATION, Retryability.USER_ACTION, default_action="Ensure /config and /data exist and are writable (PUID/PGID).", http_status=503),
    "STORAGE_READ_ONLY": ErrorSpec(ErrorCategory.STORAGE, ErrorScope.APPLICATION, Retryability.USER_ACTION, default_action="Mount /config and /data read-write.", http_status=503),
    "STORAGE_FULL": ErrorSpec(ErrorCategory.STORAGE, ErrorScope.APPLICATION, Retryability.TRANSIENT, default_action="Free disk space or inodes; the app will retry on the next run.", http_status=507),
    "STORAGE_CORRUPT": ErrorSpec(ErrorCategory.STATE_INTEGRITY, ErrorScope.APPLICATION, Retryability.USER_ACTION, outcome=Outcome.RECOVERY_REQUIRED, default_action="Restore a backup of /data or follow docs/recovery.md; mutations are disabled.", http_status=503),
    "STATE_SCHEMA_UNSUPPORTED": ErrorSpec(ErrorCategory.STATE_INTEGRITY, ErrorScope.APPLICATION, Retryability.NEVER, outcome=Outcome.RECOVERY_REQUIRED, default_action="This state was written by a newer version. Upgrade the app; do not downgrade.", http_status=503),
    "STATE_MIGRATION_FAILED": ErrorSpec(ErrorCategory.STATE_INTEGRITY, ErrorScope.APPLICATION, Retryability.USER_ACTION, outcome=Outcome.RECOVERY_REQUIRED, default_action="Restore the automatic pre-migration backup and retry.", http_status=503),
    "STATE_IDENTITY_MISMATCH": ErrorSpec(ErrorCategory.STATE_INTEGRITY, ErrorScope.APPLICATION, Retryability.USER_ACTION, outcome=Outcome.RECOVERY_REQUIRED, default_action="The /data mount does not match this installation. Re-attach the original state or run the explicit re-init workflow.", http_status=503),
    "STATE_MISSING_MOUNT": ErrorSpec(ErrorCategory.STATE_INTEGRITY, ErrorScope.APPLICATION, Retryability.USER_ACTION, outcome=Outcome.RECOVERY_REQUIRED, default_action="An existing deployment's /data appears empty. Re-attach it; do not start fresh.", http_status=503),
    "STATE_LOCK_TIMEOUT": ErrorSpec(ErrorCategory.STORAGE, ErrorScope.APPLICATION, Retryability.TRANSIENT, default_action="Another sync may be running. Retry shortly.", http_status=409),
    "STATE_NOT_INITIALIZED": ErrorSpec(ErrorCategory.STATE_INTEGRITY, ErrorScope.APPLICATION, Retryability.USER_ACTION, default_action="Run the explicit initialisation/recovery workflow to adopt this state.", http_status=409),
    # connectivity
    "CONNECT_DNS": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="Check the URL/hostname and that both containers share a Docker network.", http_status=502),
    "CONNECT_REFUSED": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="Ensure the server is running and the port is correct.", http_status=502),
    "CONNECT_TIMEOUT": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="Check network latency/load; retries are scheduled automatically.", http_status=504),
    "CONNECT_TLS": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.USER_ACTION, outcome=Outcome.NOT_STARTED, default_action="Fix the certificate or set ca_bundle; do not disable verification permanently.", http_status=502),
    "CONNECT_HTTP": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="Verify the base URL and any reverse-proxy path prefix.", http_status=502),
    "BACKEND_UNAVAILABLE": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="The backend is temporarily unavailable; automatic checks will resume.", http_status=503),
    "HTTP_REDIRECT_LOOP": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.USER_ACTION, outcome=Outcome.NOT_STARTED, default_action="Fix the reverse proxy so it does not redirect in a loop.", http_status=502),
    "HTTP_AUTH": ErrorSpec(ErrorCategory.AUTHENTICATION, ErrorScope.SERVER, Retryability.NEVER, outcome=Outcome.NOT_STARTED, default_action="Update the credentials for this server.", http_status=401),
    "HTTP_FORBIDDEN": ErrorSpec(ErrorCategory.AUTHORIZATION, ErrorScope.SERVER, Retryability.NEVER, outcome=Outcome.NOT_STARTED, default_action="Grant the account permission for this operation.", http_status=403),
    "HTTP_RATE_LIMIT": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="Backing off automatically; check for other clients hammering the server.", http_status=429),
    "HTTP_SERVER_ERROR": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="The server is unhealthy; retries are scheduled automatically.", http_status=502),
    "HTTP_MAINTENANCE": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="The server is in maintenance; retries are scheduled automatically.", http_status=503),
    "RESPONSE_MALFORMED": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.TRANSIENT, outcome=Outcome.NOT_STARTED, default_action="The server returned an unexpected response; retries are scheduled automatically.", http_status=502),
    "RESPONSE_NOT_API": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.USER_ACTION, outcome=Outcome.NOT_STARTED, default_action="A login/HTML page was returned. Check the URL and authentication.", http_status=502),
    "RESPONSE_TOO_LARGE": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.NEVER, outcome=Outcome.NOT_STARTED, default_action="The response exceeded the configured limit; raise the limit only if appropriate.", http_status=502),
    "SUBSONIC_ERROR": ErrorSpec(ErrorCategory.BACKEND_CAPABILITY, ErrorScope.SERVER, Retryability.NEVER, outcome=Outcome.NOT_STARTED, default_action="See the server message; adjust the request or the server.", http_status=502),
    # capabilities / matching / conflict
    "SMART_PLAYLIST_READONLY": ErrorSpec(ErrorCategory.BACKEND_CAPABILITY, ErrorScope.MAPPING, Retryability.NEVER, default_action="Do not sync into a smart Plex playlist; choose a static one.", http_status=409),
    "EMPTY_CREATE_UNSUPPORTED": ErrorSpec(ErrorCategory.BACKEND_CAPABILITY, ErrorScope.MAPPING, Retryability.NEVER, default_action="Add at least one matchable track before creating this target.", http_status=409),
    "CAPABILITY_UNSUPPORTED": ErrorSpec(ErrorCategory.BACKEND_CAPABILITY, ErrorScope.MAPPING, Retryability.USER_ACTION, default_action="Change the mapping so it does not require this unsupported operation.", http_status=409),
    "PLAYLIST_NOT_FOUND": ErrorSpec(ErrorCategory.BACKEND_CAPABILITY, ErrorScope.MAPPING, Retryability.NEVER, default_action="The playlist no longer exists on that server; the mapping was paused.", http_status=404),
    "MATCH_UNMATCHED": ErrorSpec(ErrorCategory.MATCHING, ErrorScope.MAPPING, Retryability.USER_ACTION, default_action="Add an explicit track mapping, or opt into partial sync.", http_status=409),
    "MATCH_AMBIGUOUS": ErrorSpec(ErrorCategory.MATCHING, ErrorScope.MAPPING, Retryability.USER_ACTION, default_action="Resolve the ambiguity by adding an explicit track mapping.", http_status=409),
    "CONFLICT_DETECTED": ErrorSpec(ErrorCategory.CONFLICT, ErrorScope.MAPPING, Retryability.USER_ACTION, outcome=Outcome.CONFLICT, default_action="Resolve the conflict by choosing a side or editing manually.", http_status=409),
    "CONFIRMATION_REQUIRED": ErrorSpec(ErrorCategory.REMOTE_MUTATION, ErrorScope.MAPPING, Retryability.USER_ACTION, outcome=Outcome.NOT_STARTED, default_action="Review the preview and confirm this destructive change.", http_status=409),
    "CONFIRMATION_STALE": ErrorSpec(ErrorCategory.REMOTE_MUTATION, ErrorScope.MAPPING, Retryability.USER_ACTION, outcome=Outcome.NOT_STARTED, default_action="The plan changed; review the new preview and confirm again.", http_status=409),
    # mutation outcomes
    "CONCURRENT_EDIT": ErrorSpec(ErrorCategory.REMOTE_MUTATION, ErrorScope.OPERATION, Retryability.AFTER_RECONCILE, outcome=Outcome.FAILED_SAFE, default_action="A playlist changed after planning; the mapping was paused. Re-run to replan.", http_status=409),
    "MUTATION_OUTCOME_UNKNOWN": ErrorSpec(ErrorCategory.REMOTE_MUTATION, ErrorScope.OPERATION, Retryability.AFTER_RECONCILE, outcome=Outcome.OUTCOME_UNKNOWN, default_action="Wait for the backend, then reconcile; do not retry blindly.", http_status=503),
    "MUTATION_PARTIAL": ErrorSpec(ErrorCategory.REMOTE_MUTATION, ErrorScope.OPERATION, Retryability.AFTER_RECONCILE, outcome=Outcome.PARTIALLY_APPLIED, default_action="Reconcile or restore from the pre-write snapshot.", http_status=502),
    "VERIFICATION_FAILED": ErrorSpec(ErrorCategory.REMOTE_MUTATION, ErrorScope.OPERATION, Retryability.AFTER_RECONCILE, outcome=Outcome.VERIFICATION_FAILED, default_action="Inspect the target and restore from snapshot if needed.", http_status=502),
    "RECOVERY_REQUIRED": ErrorSpec(ErrorCategory.REMOTE_MUTATION, ErrorScope.OPERATION, Retryability.USER_ACTION, outcome=Outcome.RECOVERY_REQUIRED, default_action="Use the recovery actions; automatic writes are blocked for this mapping.", http_status=409),
    "RESTORE_UNSAFE": ErrorSpec(ErrorCategory.REMOTE_MUTATION, ErrorScope.OPERATION, Retryability.USER_ACTION, outcome=Outcome.RECOVERY_REQUIRED, default_action="Some tracks in this snapshot no longer exist or are ambiguous; resolve them before restoring.", http_status=409),
    # resources / run control
    "RESOURCE_LIMIT": ErrorSpec(ErrorCategory.STATE_INTEGRITY, ErrorScope.MAPPING, Retryability.USER_ACTION, default_action="Raise the relevant limit or reduce the playlist size; the write was blocked.", http_status=409),
    "PAGINATION_LOOP": ErrorSpec(ErrorCategory.CONNECTIVITY, ErrorScope.SERVER, Retryability.NEVER, outcome=Outcome.NOT_STARTED, default_action="The server repeated a page; results were not treated as complete.", http_status=502),
    "RUN_IN_PROGRESS": ErrorSpec(ErrorCategory.CANCELLATION, ErrorScope.APPLICATION, Retryability.TRANSIENT, outcome=Outcome.SKIPPED, default_action="A sync is already running; wait for it to finish.", http_status=409),
    "CANCELLED": ErrorSpec(ErrorCategory.CANCELLATION, ErrorScope.OPERATION, Retryability.USER_ACTION, outcome=Outcome.CANCELLED, default_action="Reconcile before starting again if a mutation was in progress.", http_status=409),
    "INTERNAL": ErrorSpec(ErrorCategory.INTERNAL, ErrorScope.APPLICATION, Retryability.USER_ACTION, outcome=Outcome.NOT_STARTED, default_action="Export diagnostics and report the issue; include the error id.", http_status=500),
}


@dataclass
class AppError(Exception):
    code: str = "INTERNAL"
    message: str = "An unexpected error occurred."
    action: str = ""
    category: ErrorCategory = ErrorCategory.INTERNAL
    scope: ErrorScope = ErrorScope.APPLICATION
    retryability: Retryability = Retryability.NEVER
    outcome: Outcome = Outcome.NOT_STARTED
    http_status: int = 500
    retry_after: float | None = None
    operation_id: str | None = None
    run_id: int | None = None
    mapping_id: str | None = None
    backend: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    cause_type: str | None = None

    def __post_init__(self) -> None:
        spec = ERROR_SPECS.get(self.code)
        if spec is not None:
            # Fill in category/scope/retryability/outcome/action from the spec
            # unless the caller overrode them deliberately.
            if self.category is ErrorCategory.INTERNAL:
                self.category = spec.category
            if self.scope is ErrorScope.APPLICATION and spec.scope is not ErrorScope.APPLICATION:
                self.scope = spec.scope
            if self.retryability is Retryability.NEVER:
                self.retryability = spec.retryability
            if self.outcome is Outcome.NOT_STARTED and spec.outcome is not Outcome.NOT_STARTED:
                self.outcome = spec.outcome
            if not self.action:
                self.action = spec.default_action
            if self.http_status == 500:
                self.http_status = spec.http_status
        Exception.__init__(self, self.message)

    def __str__(self) -> str:
        # Safe message only; never includes secrets or ids.
        return self.message

    def to_dict(self, *, include_details: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "category": self.category.value,
            "scope": self.scope.value,
            "message": self.message,
            "action": self.action,
            "retryability": self.retryability.value,
            "outcome": self.outcome.value,
            "retry_after": self.retry_after,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "mapping_id": self.mapping_id,
            "backend": self.backend,
        }
        if include_details:
            out["details"] = sanitize_details(self.details)
            out["cause_type"] = self.cause_type
        return out


def sanitize_details(details: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (details or {}).items():
        if isinstance(value, str):
            out[key] = scrub(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = scrub(str(value))
    return out


def make_error(code: str, message: str, **kwargs: Any) -> AppError:
    return AppError(code=code, message=message, **kwargs)


def wrap_exception(
    exc: BaseException,
    *,
    backend: str | None = None,
    scope: ErrorScope | None = None,
    default_code: str = "INTERNAL",
    default_message: str = "An unexpected error occurred.",
    **kwargs: Any,
) -> AppError:
    """Convert any exception (including low-level backend errors) to AppError."""
    if isinstance(exc, AppError):
        if backend and not exc.backend:
            exc.backend = backend
        return exc
    code = getattr(exc, "code", None) or default_code
    if code not in ERROR_SPECS:
        code = default_code
    message = getattr(exc, "safe_message", None) or default_message
    err = AppError(
        code=code,
        message=str(message),
        backend=backend,
        cause_type=type(exc).__name__,
        details={"technical": str(exc)},
        **kwargs,
    )
    if scope is not None and scope is not ErrorScope.APPLICATION:
        err.scope = scope
    return err


def format_traceback(exc: BaseException) -> str:
    """Scrubbed traceback for diagnostics only."""
    return scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
