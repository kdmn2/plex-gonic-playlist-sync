"""Background library-index warm-up.

Builds the Plex and Gonic library indexes shortly after startup and refreshes
them periodically, so the first interactive "Match status"/preview/merge does not
have to build a large index synchronously. Read-only, cancellable, and safe when
a backend is unavailable (errors are logged and retried next interval).
"""

from __future__ import annotations

import asyncio
import logging

from ..models import ServerKind

log = logging.getLogger("playlist_sync.warmup")


class IndexWarmer:
    def __init__(self, ctx):
        self.ctx = ctx
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="library-index-warmer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        # small delay so startup stays responsive
        if await self._sleep(3):
            return
        first = True
        while True:
            for side in (ServerKind.PLEX, ServerKind.GONIC):
                if self._stop.is_set():
                    return
                try:
                    log.info("warm-up: building %s library index", side.value)
                    await asyncio.to_thread(self.ctx.library_index, side, refresh=not first)
                except Exception as exc:
                    log.warning("warm-up: %s index build failed: %s", side.value, exc)
            first = False
            interval = self.ctx.settings.limits.index_refresh_seconds
            if interval <= 0:
                log.info("warm-up: periodic refresh disabled; single warm-up complete")
                return
            if await self._sleep(interval):
                return

    async def _sleep(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop.is_set()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False
