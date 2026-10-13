"""Guarded application of a plan.

Order of operations:
  1. refuse if the plan is blocked / conflicted / a no-op
  2. re-fetch the affected playlists and abort if they changed after planning
  3. take a pre-write snapshot of the target
  4. record a journal entry *before* the remote write
  5. mutate conservatively
  6. read back and verify order + duplicates
  7. only then mark success and save the last-successful snapshot

On timeout/transport failure we inspect remote state before doing anything else;
we never blindly retry a create or append.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..backends import BackendUnavailable, PlaylistNotFound
from ..models import RemotePlaylist, ServerKind
from ..store.db import utcnow
from .context import EngineContext
from .planner import Plan
from .verify import verify_sequence

log = logging.getLogger("playlist_sync.engine.apply")


class ConcurrentEditError(RuntimeError):
    """A source or target changed between planning and application."""


class ApplyError(RuntimeError):
    """A remote write failed or could not be verified."""

    def __init__(self, message: str, *, partial: bool = False, snapshot_id: int | None = None):
        super().__init__(message)
        self.partial = partial
        self.snapshot_id = snapshot_id


@dataclass
class ApplyResult:
    status: str
    detail: str = ""
    verified: bool = False
    read_back: RemotePlaylist | None = None
    snapshot_id: int | None = None
    journal_id: int | None = None
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
) -> ApplyResult:
    if plan.conflict:
        return ApplyResult(status="conflict", detail="; ".join(plan.blocked_reasons))
    if plan.blocked:
        return ApplyResult(status="blocked", detail="; ".join(plan.blocked_reasons))
    if plan.no_change:
        return ApplyResult(status="no_change", detail="already in sync")

    target_side = plan.write_side or plan.target_side
    backend = ctx.backend(target_side)

    # -- concurrency re-check --------------------------------------------------
    _recheck(ctx, plan)

    # -- snapshot --------------------------------------------------------------
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

    journal_payload = {
        "target_side": target_side.value,
        "create": plan.create_target,
        "rename_to": plan.rename_to,
        "desired_count": len(plan.desired_ids),
        "expected_ids": plan.desired_ids[:200],
        "trigger": trigger,
    }
    journal_id = ctx.store.journal_start(
        run_id, plan.mapping.id, target_side.value, "replace_entries", journal_payload
    )

    target_id = plan.mapping.gonic_playlist_id if target_side == ServerKind.GONIC else plan.mapping.plex_playlist_id

    try:
        if plan.create_target:
            new_id = backend.create_playlist(plan.rename_to or plan.source_playlist.summary.name, plan.desired_ids)  # type: ignore[union-attr]
            if target_side == ServerKind.GONIC:
                ctx.store.update_mapping(plan.mapping.id, gonic_playlist_id=new_id, gonic_name=plan.rename_to or plan.source_playlist.summary.name)  # type: ignore[union-attr]
            else:
                ctx.store.update_mapping(plan.mapping.id, plex_playlist_id=new_id, plex_name=plan.rename_to or plan.source_playlist.summary.name)  # type: ignore[union-attr]
            target_id = new_id
        else:
            assert target_id is not None
            backend.replace_playlist_entries(target_id, plan.desired_ids)
            if plan.rename_to:
                backend.rename_playlist(target_id, plan.rename_to)
    except BackendUnavailable as exc:
        return _handle_uncertain(ctx, plan, target_side, target_id, journal_id, snapshot_id, str(exc))
    except Exception as exc:
        ctx.store.journal_finish(journal_id, "failed", detail=str(exc))
        raise ApplyError(f"target mutation failed: {exc}", partial=True, snapshot_id=snapshot_id) from exc

    ctx.store.journal_finish(journal_id, "written", detail="awaiting verification")

    # -- verify ----------------------------------------------------------------
    if target_id is None:
        raise ApplyError("no target playlist id after write", partial=True, snapshot_id=snapshot_id)
    read_back = _read_back(ctx, target_side, target_id)
    if read_back is None:
        ctx.store.journal_finish(journal_id, "failed", detail="target missing after write")
        raise ApplyError(
            "target playlist could not be read back after write", partial=True, snapshot_id=snapshot_id
        )

    ok, detail = verify_sequence(read_back, plan.desired_ids)
    if not ok:
        ctx.store.journal_finish(journal_id, "failed", detail=detail)
        raise ApplyError(f"verification failed: {detail}", partial=True, snapshot_id=snapshot_id)

    name_ok = True
    if plan.rename_to:
        name_ok = read_back.summary.name == plan.rename_to
        if not name_ok:
            ctx.store.journal_finish(journal_id, "failed", detail="rename not reflected on read-back")
            raise ApplyError(
                f"rename verification failed: expected {plan.rename_to!r}, got {read_back.summary.name!r}",
                partial=True,
                snapshot_id=snapshot_id,
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
    )


def _recheck(ctx: EngineContext, plan: Plan) -> None:
    """Abort if the source (or the target before mutation) changed after planning."""
    if plan.source_playlist is not None:
        fresh = _read_back(ctx, plan.source_side, plan.source_playlist.playlist_id)
        if fresh is None or fresh.fingerprint() != plan.source_playlist.fingerprint():
            ctx.store.update_mapping(plan.mapping.id, paused=True)
            raise ConcurrentEditError(
                "source playlist changed after planning; aborting write and pausing mapping"
            )
    target_id = (
        plan.mapping.gonic_playlist_id if plan.target_side == ServerKind.GONIC else plan.mapping.plex_playlist_id
    )
    if plan.create_target:
        existing = _read_back(ctx, plan.target_side, target_id) if target_id else None
        if existing is not None:
            raise ConcurrentEditError("target playlist was created concurrently; aborting")
    elif plan.target_playlist is not None:
        fresh = _read_back(ctx, plan.target_side, plan.target_playlist.playlist_id)
        if fresh is None or fresh.fingerprint() != plan.target_playlist.fingerprint():
            raise ConcurrentEditError(
                "target playlist changed after planning; aborting write and replanning is required"
            )


def _handle_uncertain(
    ctx: EngineContext,
    plan: Plan,
    target_side: ServerKind,
    target_id: str | None,
    journal_id: int,
    snapshot_id: int | None,
    error: str,
) -> ApplyResult:
    """A transport error occurred mid-write. Inspect before deciding anything."""
    if target_id is None and plan.create_target:
        # Creation may or may not have happened. Look for a playlist with the name.
        found = _find_by_name(ctx, target_side, plan.rename_to or "")
        if found is None:
            ctx.store.journal_finish(
                journal_id, "unknown", detail=f"create outcome unknown after error: {error}"
            )
            raise ApplyError(
                f"playlist creation outcome is unknown after transport error ({error}); "
                "inspect the target server before retrying",
                partial=True,
                snapshot_id=snapshot_id,
            )
        target_id = found.playlist_id

    if target_id is None:
        ctx.store.journal_finish(journal_id, "unknown", detail=error)
        raise ApplyError(f"write outcome unknown: {error}", partial=True, snapshot_id=snapshot_id)

    read_back = _read_back(ctx, target_side, target_id)
    if read_back is not None:
        ok, detail = verify_sequence(read_back, plan.desired_ids)
        if ok:
            ctx.store.journal_finish(
                journal_id, "verified", detail=f"recovered after timeout: {detail}"
            )
            return ApplyResult(
                status="applied_verified",
                detail=f"recovered after transport error: {detail}",
                verified=True,
                read_back=read_back,
                snapshot_id=snapshot_id,
                journal_id=journal_id,
                replayed=True,
            )
    ctx.store.journal_finish(journal_id, "unknown", detail=error)
    raise ApplyError(
        f"write could not be verified after transport error ({error}); snapshot "
        f"{snapshot_id} is available for restore; inspect the server before retrying",
        partial=True,
        snapshot_id=snapshot_id,
    )


def _find_by_name(ctx: EngineContext, side: ServerKind, name: str) -> RemotePlaylist | None:
    if not name:
        return None
    for summary in ctx.backend(side).list_playlists(audio_only=False):
        if summary.name == name:
            return _read_back(ctx, side, summary.playlist_id)
    return None


def restore_snapshot(
    ctx: EngineContext, snapshot_id: int, *, run_id: int | None = None, trigger: str = "restore"
) -> ApplyResult:
    """Explicitly restore a target playlist from a stored snapshot."""
    snapshot = ctx.store.get_snapshot(snapshot_id)
    if not snapshot:
        raise ApplyError(f"snapshot {snapshot_id} not found")
    side = ServerKind(snapshot["side"])
    playlist_id = snapshot["playlist_id"]
    entries = json.loads(snapshot["entries"])
    track_ids = [e["track_id"] for e in entries]
    backend = ctx.backend(side)

    journal_id = ctx.store.journal_start(
        run_id,
        snapshot.get("mapping_id") or "",
        side.value,
        "restore_from_snapshot",
        {"snapshot_id": snapshot_id, "count": len(track_ids)},
    )
    if playlist_id:
        backend.replace_playlist_entries(playlist_id, track_ids)
    else:
        playlist_id = backend.create_playlist(snapshot.get("name") or "restored", track_ids)
    ctx.store.journal_finish(journal_id, "written", detail="restore awaiting verification")

    read_back = _read_back(ctx, side, playlist_id) if playlist_id else None
    if read_back is None:
        raise ApplyError("restored playlist could not be read back", partial=True)
    ok, detail = verify_sequence(read_back, track_ids)
    if not ok:
        raise ApplyError(f"restore verification failed: {detail}", partial=True)
    ctx.store.journal_finish(journal_id, "verified", detail=detail)
    return ApplyResult(
        status="restored",
        detail=detail,
        verified=True,
        read_back=read_back,
        journal_id=journal_id,
    )


__all__ = [
    "ApplyError",
    "ApplyResult",
    "ConcurrentEditError",
    "apply_plan",
    "restore_snapshot",
    "utcnow",
]
