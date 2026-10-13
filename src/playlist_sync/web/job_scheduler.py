"""Unified recurring-operation scheduler.

One loop drives every job type (append, copy, merge, backup, sync). It reuses
the process-safe executor lock so jobs with shared writable resources never
compete, coalesces overlapping occurrences instead of queueing, and performs at
most one catch-up run after downtime (next occurrence is always computed from
"now", never from the missed time).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from ..engine.jobs import compute_next_run, occurrence_id, run_job
from ..engine.reconcile import reconcile_operations
from ..store.db import utcnow

log = logging.getLogger("playlist_sync.jobs.scheduler")

SUCCESS_STATES = {
    "applied_verified", "no_change", "appended", "merged", "copied",
    "copied_mapping_enabled", "copied_mapping_paused", "ok", "already_appended",
    "already_merged", "already_copied", "empty",
}


class JobScheduler:
    def __init__(self, ctx, poll_seconds: int = 30):
        self.ctx = ctx
        self.poll_seconds = max(5, poll_seconds)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="jobs-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        if await self._sleep(5):
            return
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("job scheduler tick failed")
            if await self._sleep(self.poll_seconds):
                return

    async def tick_once(self) -> list[dict]:
        """Run one scheduler pass (also used by tests)."""
        return await self._tick()

    async def _tick(self) -> list[dict]:
        if not self.ctx.has_store or not self.ctx.storage_ready:
            return []
        now = datetime.now(UTC)
        outcomes: list[dict] = []
        for job in self.ctx.store.due_jobs(now.isoformat()):
            outcomes.append(await self._run_job(job, now))
        return outcomes

    async def _run_job(self, job: dict, now: datetime) -> dict:
        scheduled = _parse_iso(job.get("next_run"))
        occ = occurrence_id(job["id"], scheduled) if scheduled else None
        if occ and job.get("last_occurrence") == occ:
            # already executed this occurrence; just advance
            self._advance(job, now)
            return {"job": job["id"], "status": "already_ran"}

        owner = f"job-{job['id']}"
        if not self.ctx.store.acquire_lock("sync", owner, ttl_seconds=1800, wait_seconds=0):
            log.info("job %s coalesced (executor busy)", job["name"])
            self.ctx.store.update_job(
                job["id"], last_attempt=utcnow(), last_occurrence=occ,
                last_result=json.dumps({"status": "coalesced", "detail": "another operation was running"}),
            )
            self._advance(job, now)
            return {"job": job["id"], "status": "coalesced"}

        try:
            await asyncio.to_thread(reconcile_operations, self.ctx)
            run_id = self.ctx.store.start_run(f"job:{job['id']}", job.get("type") or "job", "schedule")
            run = await asyncio.to_thread(run_job, self.ctx, job)
            self.ctx.store.finish_run(
                run_id, run.status, stats=run.extra or {},
                error=run.detail if run.status not in SUCCESS_STATES else "",
            )
            fields = {
                "last_attempt": utcnow(),
                "last_occurrence": occ,
                "last_result": json.dumps({"status": run.status, "detail": run.detail, **(run.extra or {})}),
            }
            if run.status in SUCCESS_STATES:
                fields["last_success"] = utcnow()
            self.ctx.store.update_job(job["id"], **fields)
            log.info("job %s -> %s (%s)", job["name"], run.status, run.detail)
            result = {"job": job["id"], "status": run.status, "detail": run.detail}
        finally:
            self.ctx.store.release_lock("sync", owner)
        self._advance(job, now)
        return result

    def _advance(self, job: dict, now: datetime) -> None:
        nxt = compute_next_run(job.get("frequency") or {}, job.get("timezone") or "UTC", now)
        self.ctx.store.update_job(job["id"], next_run=nxt.isoformat() if nxt else None)

    async def _sleep(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stopping.is_set()
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None
