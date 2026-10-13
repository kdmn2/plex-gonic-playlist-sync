"""Background polling scheduler.

Runs the same engine as the CLI. Bounded retries with exponential backoff;
overlap with manual/CLI syncs is prevented by the engine locks.
"""

from __future__ import annotations

import asyncio
import logging
import random

from ..engine.context import EngineContext
from ..engine.sync import sync_all

log = logging.getLogger("playlist_sync.scheduler")


class Scheduler:
    def __init__(self, ctx: EngineContext):
        self.ctx = ctx
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="playlist-sync-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        interval = max(0, self.ctx.settings.sync.poll_seconds)
        if interval <= 0:
            return
        # initial small delay so startup is responsive
        if await self._sleep(5):
            return
        while True:
            await self._tick_with_backoff()
            if await self._sleep(interval):
                return

    async def _tick_with_backoff(self) -> None:
        settings = self.ctx.settings.sync
        delay = max(0.0, settings.retry_backoff_seconds)
        for attempt in range(1, settings.max_retries + 1):
            try:
                results = await asyncio.to_thread(
                    sync_all, self.ctx, dry_run=False, trigger="schedule"
                )
                for result in results:
                    log.info(
                        "scheduled sync %s: %s (%s)",
                        result.mapping_name,
                        result.status,
                        result.detail,
                    )
                if any(r.status == "error" for r in results) and attempt < settings.max_retries:
                    raise RuntimeError("one or more mappings errored")
                return
            except Exception as exc:
                log.warning(
                    "scheduled sync attempt %s/%s failed: %s",
                    attempt,
                    settings.max_retries,
                    exc,
                )
                await self._sleep(delay + random.uniform(0, 1))
                delay *= 2

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, returning True if we were asked to stop."""
        if seconds <= 0:
            return self._stopping.is_set()
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False
