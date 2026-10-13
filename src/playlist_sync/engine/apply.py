"""Guarded application of a plan, with a durable operation lifecycle.

Lifecycle: PLANNED -> PREPARED -> APPLYING -> VERIFYING -> SUCCEEDED, plus
FAILED_SAFE, OUTCOME_UNKNOWN, PARTIALLY_APPLIED, CONFLICT, RECOVERY_REQUIRED and
CANCELLED_SAFE. The intended change, identities, fingerprints and the pre-write
snapshot are persisted *before* any remote write. If local persistence fails we
do not start the write. On an uncertain outcome we re-read remote state and
classify; we never blindly retry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..backends import BackendUnavailable, PlaylistNotFound
from ..errors import AppError, Outcome
from ..models import RemotePlaylist, ServerKind, fingerprint_ids
from ..redact import scrub
from ..store import OperationState
from .context import EngineContext
from .operations import (
    apply_outcome_to_operation,
    classify_operation,
    safe_get_playlist,
)
from .planner import Plan
from .verify import verify_sequence

log = logging.getLogger("playlist_sync.engine.apply")


class ConcurrentEditError(AppError):
    def __init__(self, message: str):
        super().__init__(code="CONCURRENT_EDIT", message=message)
        self.partial = False
        self.snapshot_id = None


class ApplyError(AppError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        outcome: Outcome | None = None,
        snapshot_id: int | None = None,
        operation_key: str | None = None,
    ):
        super().__init__(code=code, message=message)
        if outcome is not None:
            self.outcome = outcome
        self.partial = self.outcome in {
            Outcome.PARTIALLY_APPLIED,
            Outcome.OUTCOME_UNKNOWN,
            Outcome.VERIFICATION_FAILED,
        }
        self.snapshot_id = snapshot_id
        self.operation_key = operation_key


@dataclass
class ApplyResult:
    status: str
    detail: str = ""
    verified: bool = False
    read_back: RemotePlaylist | None = None
    snapshot_id: int | None = None
    journal_id: int | None = None
    operation_key: str | None = None
    partial: bool = False
    replayed: bool = False


def _snapshot_entries(playlist: RemotePlaylist) -> list[dict]:
    return [
        {"track_id": e.track.track_id, "occurrence_id": e.occurrence_id, "index": e.index}
        for e in playlist.entries
    ]


def _read_back(ctx: EngineContext, side: ServerKind, playlist_id: str) -> RemotePlaylist | None:
    try:
        return ctx.backend(side).get_playlist(playlist_id)
    except PlaylistNotFound:
        return None


def apply_plan(
    ctx: EngineContext,
    plan: Plan,
    *,
    run_id: int | None = None,
    trigger: str = "manual",
    operation_key_seed: str | None = None,
) -> ApplyResult:
    if plan.conflict:
        return ApplyResult(status="conflict", detail="; ".join(plan.blocked_reasons))
    if plan.blocked:
        return ApplyResult(status="blocked", detail="; ".join(plan.blocked_reasons))
    if plan.no_change:
        return ApplyResult(status="no_change", detail="already in sync")

    # Storage must be writable before any remote mutation.
    ctx.ensure_mutable()

    target_side = plan.write_side or plan.target_side
    backend = ctx.backend(target_side)
    desired_ids = list(plan.desired_ids)
    desired_fp = fingerprint_ids(desired_ids)
    before_fp = plan.target_playlist.fingerprint() if plan.target_playlist else None
    target_id = (
        plan.mapping.gonic_playlist_id if target_side == ServerKind.GONIC else plan.mapping.plex_playlist_id
    )
    source_fp = plan.source_playlist.fingerprint() if plan.source_playlist else None
    intended_name = plan.rename_to or (
        plan.source_playlist.summary.name if plan.source_playlist else None
    )

    # Persist the intended change BEFORE any remote write.
    operation_key = ctx.store.create_operation(
        mapping_id=plan.mapping.id,
        run_id=run_id,
        direction=plan.direction.value,
        source_side=plan.source_side.value,
        target_side=target_side.value,
        source_playlist_id=plan.source_playlist.playlist_id if plan.source_playlist else None,
        target_playlist_id=target_id if plan.target_exists else None,
        intended_name=intended_name,
        state=OperationState.PLANNED,
        step="planned",
        source_fingerprint=source_fp,
        target_fingerprint_before=before_fp,
        desired_fingerprint=desired_fp,
        desired_ids=desired_ids,
    )

    # Concurrency re-check (IS-6). On abort the target is untouched.
    try:
        _recheck(ctx, plan)
    except ConcurrentEditError as exc:
        ctx.store.update_operation(
            operation_key, state=OperationState.FAILED_SAFE, step="aborted", detail=str(exc)
        )
        raise

    snapshot_id = None
    if plan.target_exists and plan.target_playlist is not None:
        snapshot_id = ctx.store.save_snapshot(
            mapping_id=plan.mapping.id,
            side=target_side.value,
            playlist_id=plan.target_playlist.playlist_id,
            kind="pre_write",
            entries=_snapshot_entries(plan.target_playlist),
            fingerprint=plan.target_playlist.fingerprint(),
            run_id=run_id,
        )
    ctx.store.update_operation(
        operation_key, state=OperationState.PREPARED, step="prepared", snapshot_id=snapshot_id
    )
    journal_id = ctx.store.journal_start(
        run_id,
        plan.mapping.id,
        target_side.value,
        "replace_entries",
        {"create": plan.create_target, "desired_count": len(desired_ids), "trigger": trigger},
    )

    # ---- mutation -----------------------------------------------------------
    ctx.store.update_operation(operation_key, state=OperationState.APPLYING, step="applying")
    try:
        if plan.create_target:
            created_id = backend.create_playlist(intended_name or "playlist", desired_ids)
            target_id = created_id
            ctx.store.update_operation(
                operation_key,
                created_playlist_id=created_id,
                target_playlist_id=created_id,
                step="created",
            )
            if target_side == ServerKind.GONIC:
                ctx.store.update_mapping(
                    plan.mapping.id, gonic_playlist_id=created_id, gonic_name=intended_name
                )
            else:
                ctx.store.update_mapping(
                    plan.mapping.id, plex_playlist_id=created_id, plex_name=intended_name
                )
        else:
            if target_id is None:
                raise ApplyError("MUTATION_PARTIAL", "no target playlist id available")
            backend.replace_playlist_entries(target_id, desired_ids)
            if plan.rename_to:
                backend.rename_playlist(target_id, plan.rename_to)
    except (BackendUnavailable, AppError, Exception) as exc:
        return _resolve_uncertain(
            ctx, operation_key, target_side, target_id, snapshot_id, journal_id, exc
        )

    # ---- verification -------------------------------------------------------
    ctx.store.update_operation(operation_key, state=OperationState.VERIFYING, step="verifying")
    read_back = _read_back(ctx, target_side, target_id) if target_id else None
    if read_back is None:
        ctx.store.update_operation(
            operation_key, state=OperationState.RECOVERY_REQUIRED, step="verify", detail="target missing after write"
        )
        ctx.store.journal_finish(journal_id, "failed", detail="target missing after write")
        raise ApplyError(
            "MUTATION_PARTIAL",
            "The target playlist could not be read back after the write.",
            snapshot_id=snapshot_id,
            operation_key=operation_key,
        )
    ok, detail = verify_sequence(read_back, desired_ids)
    if not ok:
        ctx.store.update_operation(
            operation_key, state=OperationState.RECOVERY_REQUIRED, step="verify", detail=detail
        )
        ctx.store.journal_finish(journal_id, "failed", detail=detail)
        raise ApplyError(
            "VERIFICATION_FAILED",
            f"Verification failed: {detail}",
            snapshot_id=snapshot_id,
            operation_key=operation_key,
        )
    if plan.rename_to and read_back.summary.name != plan.rename_to:
        ctx.store.update_operation(
            operation_key, state=OperationState.RECOVERY_REQUIRED, step="verify", detail="rename not reflected"
        )
        raise ApplyError(
            "VERIFICATION_FAILED",
            f"Rename verification failed: expected {plan.rename_to!r}, got {read_back.summary.name!r}",
            snapshot_id=snapshot_id,
            operation_key=operation_key,
        )

    ctx.store.update_operation(
        operation_key,
        state=OperationState.SUCCEEDED,
        step="verified",
        target_fingerprint_after=read_back.fingerprint(),
    )
    ctx.store.journal_finish(journal_id, "verified", detail=detail)
    ctx.store.save_snapshot(
        mapping_id=plan.mapping.id,
        side=target_side.value,
        playlist_id=read_back.playlist_id,
        kind="last_success",
        entries=_snapshot_entries(read_back),
        fingerprint=read_back.fingerprint(),
        run_id=run_id,
    )
    ctx.store.update_mapping(plan.mapping.id, paused=False)
    return ApplyResult(
        status="applied_verified",
        detail=detail,
        verified=True,
        read_back=read_back,
        snapshot_id=snapshot_id,
        journal_id=journal_id,
        operation_key=operation_key,
    )


def _resolve_uncertain(
    ctx: EngineContext,
    operation_key: str,
    target_side: ServerKind,
    target_id: str | None,
    snapshot_id: int | None,
    journal_id: int | None,
    exc: Exception,
) -> ApplyResult:
    """A write failed or timed out. Re-read remote state and classify; never retry blindly."""
    detail = scrub(str(exc))
    code = getattr(exc, "code", "INTERNAL")
    ctx.store.update_operation(
        operation_key,
        state=OperationState.OUTCOME_UNKNOWN,
        step="outcome_unknown",
        error_code=code,
        detail=detail,
    )
    op = ctx.store.get_operation(operation_key)
    try:
        outcome = classify_operation(ctx, op) if op else None
    except Exception as reconcile_exc:
        outcome = None
        detail = f"{detail}; reconciliation pending: {scrub(str(reconcile_exc))}"

    if outcome is None:
        if journal_id:
            ctx.store.journal_finish(journal_id, "unknown", detail=detail)
        raise ApplyError(
            "MUTATION_OUTCOME_UNKNOWN",
            "The result of the playlist update is not yet known; it will be reconciled when the "
            "backend is reachable. The mapping was paused to avoid duplicate changes.",
            outcome=Outcome.OUTCOME_UNKNOWN,
            snapshot_id=snapshot_id,
            operation_key=operation_key,
        )

    apply_outcome_to_operation(ctx, op, outcome)
    if journal_id:
        ctx.store.journal_finish(journal_id, "reconciled", detail=outcome.detail)

    if outcome.state == OperationState.SUCCEEDED and outcome.read_back is not None:
        ctx.store.save_snapshot(
            mapping_id=op["mapping_id"],
            side=target_side.value,
            playlist_id=outcome.read_back.playlist_id,
            kind="last_success",
            entries=_snapshot_entries(outcome.read_back),
            fingerprint=outcome.read_back.fingerprint(),
        )
        return ApplyResult(
            status="applied_verified",
            detail=f"recovered after {code}: {outcome.detail}",
            verified=True,
            read_back=outcome.read_back,
            snapshot_id=snapshot_id,
            operation_key=operation_key,
            replayed=True,
        )
    if outcome.state == OperationState.FAILED_SAFE:
        raise ApplyError(
            "CONCURRENT_EDIT" if code == "CONCURRENT_EDIT" else "MUTATION_PARTIAL",
            "The playlist update did not take effect; the target is unchanged.",
            outcome=Outcome.FAILED_SAFE,
            snapshot_id=snapshot_id,
            operation_key=operation_key,
        )
    raise ApplyError(
        "RECOVERY_REQUIRED",
        outcome.detail,
        outcome=Outcome.RECOVERY_REQUIRED if outcome.needs_intervention else Outcome.PARTIALLY_APPLIED,
        snapshot_id=snapshot_id,
        operation_key=operation_key,
    )


def _recheck(ctx: EngineContext, plan: Plan) -> None:
    """Abort if the source (or the target before mutation) changed after planning (IS-6)."""
    if plan.source_playlist is not None:
        fresh = _read_back(ctx, plan.source_side, plan.source_playlist.playlist_id)
        if fresh is None or fresh.fingerprint() != plan.source_playlist.fingerprint():
            ctx.store.update_mapping(plan.mapping.id, paused=True)
            raise ConcurrentEditError(
                "The source playlist changed after planning; the write was aborted and the "
                "mapping paused. Re-run to replan."
            )
    target_id = (
        plan.mapping.gonic_playlist_id if plan.target_side == ServerKind.GONIC else plan.mapping.plex_playlist_id
    )
    if plan.create_target:
        existing = _read_back(ctx, plan.target_side, target_id) if target_id else None
        if existing is not None:
            raise ConcurrentEditError("The target playlist was created concurrently; the write was aborted.")
    elif plan.target_playlist is not None:
        fresh = _read_back(ctx, plan.target_side, plan.target_playlist.playlist_id)
        if fresh is None or fresh.fingerprint() != plan.target_playlist.fingerprint():
            raise ConcurrentEditError(
                "The target playlist changed after planning; the write was aborted. Re-run to replan."
            )


def restore_snapshot(
    ctx: EngineContext, snapshot_id: int, *, run_id: int | None = None, trigger: str = "restore"
) -> ApplyResult:
    """Explicitly restore a target playlist from a stored snapshot.

    Validates that referenced tracks still exist and are unambiguously present
    before writing. Never recreates a deleted playlist silently.
    """
    ctx.ensure_mutable()
    snapshot = ctx.store.get_snapshot(snapshot_id)
    if not snapshot:
        raise ApplyError("RECOVERY_REQUIRED", f"snapshot {snapshot_id} not found")
    side = ServerKind(snapshot["side"])
    playlist_id = snapshot["playlist_id"]
    entries = json.loads(snapshot["entries"])
    track_ids = [e["track_id"] for e in entries]

    current = safe_get_playlist(ctx, side, playlist_id) if playlist_id else None
    if playlist_id and current is None:
        raise ApplyError(
            "RESTORE_UNSAFE",
            "The target playlist no longer exists. Restoring it would recreate a deleted playlist; "
            "resolve this manually.",
        )

    index = ctx.library_index(side)
    missing = [tid for tid in track_ids if index.get(tid) is None]
    if missing:
        raise ApplyError(
            "RESTORE_UNSAFE",
            f"{len(missing)} track(s) in this snapshot no longer exist on {side.value}; resolve them first.",
        )

    operation_key = ctx.store.create_operation(
        mapping_id=snapshot.get("mapping_id"),
        run_id=run_id,
        direction="restore",
        source_side=side.value,
        target_side=side.value,
        target_playlist_id=playlist_id,
        state=OperationState.PLANNED,
        step="restore_planned",
        desired_fingerprint=fingerprint_ids(track_ids),
        desired_ids=track_ids,
    )
    journal_id = ctx.store.journal_start(
        run_id, snapshot.get("mapping_id") or "", side.value, "restore_from_snapshot",
        {"snapshot_id": snapshot_id, "count": len(track_ids)},
    )
    ctx.store.update_operation(operation_key, state=OperationState.APPLYING, step="restoring")
    backend = ctx.backend(side)
    try:
        backend.replace_playlist_entries(playlist_id, track_ids)
    except Exception as exc:
        return _resolve_uncertain(ctx, operation_key, side, playlist_id, snapshot_id, journal_id, exc)

    ctx.store.update_operation(operation_key, state=OperationState.VERIFYING, step="restore_verifying")
    read_back = _read_back(ctx, side, playlist_id) if playlist_id else None
    if read_back is None:
        ctx.store.update_operation(
            operation_key, state=OperationState.RECOVERY_REQUIRED, step="restore_verify"
        )
        raise ApplyError("RESTORE_UNSAFE", "The restored playlist could not be read back.", operation_key=operation_key)
    ok, detail = verify_sequence(read_back, track_ids)
    if not ok:
        ctx.store.update_operation(
            operation_key, state=OperationState.RECOVERY_REQUIRED, step="restore_verify", detail=detail
        )
        raise ApplyError("RESTORE_UNSAFE", f"Restore verification failed: {detail}", operation_key=operation_key)
    ctx.store.update_operation(operation_key, state=OperationState.SUCCEEDED, step="restored")
    ctx.store.journal_finish(journal_id, "verified", detail=detail)
    return ApplyResult(
        status="restored",
        detail=detail,
        verified=True,
        read_back=read_back,
        journal_id=journal_id,
        operation_key=operation_key,
    )
