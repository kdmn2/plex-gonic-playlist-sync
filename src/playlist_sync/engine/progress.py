"""Shared operation-progress model and throttled reporter.

Stage/counter semantics (documented for the UI and tests):
- ``stage`` / ``stage_desc``  the current step of the operation.
- ``completed`` / ``total`` / ``unit``  stage work done (nullable total = unknown).
- ``counters``  outcome counters, e.g. read, matched, skipped, planned,
  submitted, confirmed, verified, failed. ``submitted`` is never treated as
  ``confirmed``.
- ``heartbeat_at``  proves the worker is alive; it never advances progress.
- ``revision``  monotonically increases so stale UI updates can be ignored.
"""

from __future__ import annotations

import logging
import threading
import time

from ..store.db import utcnow

log = logging.getLogger("playlist_sync.progress")


class OperationCancelled(Exception):
    """Raised at a safe point when the operator requested cancellation."""


class ProgressReporter:
    def __init__(self, store, operation_key: str, *, min_interval: float = 0.5):
        self.store = store
        self.key = operation_key
        self.min_interval = max(0.0, min_interval)
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._last_flush = 0.0

    # -- public API ------------------------------------------------------------

    def stage(
        self,
        stage: str,
        desc: str | None = None,
        *,
        completed: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        counters: dict | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            self._pending["stage"] = stage
            if desc is not None:
                self._pending["stage_desc"] = desc
            if completed is not None:
                self._pending["completed"] = completed
            if total is not None:
                self._pending["total"] = total
            if unit is not None:
                self._pending["unit"] = unit
            if counters is not None:
                self._pending["counters"] = dict(counters)
            if message is not None:
                self._pending["message"] = message
        self._flush(force=True)

    def progress(
        self,
        *,
        completed: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        counters: dict | None = None,
        message: str | None = None,
        desc: str | None = None,
        force: bool = False,
    ) -> None:
        with self._lock:
            if completed is not None:
                self._pending["completed"] = completed
            if total is not None:
                self._pending["total"] = total
            if unit is not None:
                self._pending["unit"] = unit
            if counters is not None:
                self._pending["counters"] = dict(counters)
            if message is not None:
                self._pending["message"] = message
            if desc is not None:
                self._pending["stage_desc"] = desc
        self._flush(force=force)

    def set_state(self, state: str, message: str | None = None) -> None:
        with self._lock:
            self._pending["state"] = state
            self._pending["step"] = state
            if message is not None:
                self._pending["message"] = message
        self._flush(force=True)

    def heartbeat(self) -> None:
        """Prove the worker is alive without advancing progress."""
        import contextlib

        with contextlib.suppress(Exception):
            self.store.operation_progress(self.key, heartbeat_at=utcnow())

    def finish(self, state: str, message: str = "", **extra) -> None:
        with self._lock:
            self._pending["state"] = state
            self._pending["step"] = state
            self._pending["message"] = message
            self._pending["heartbeat_at"] = utcnow()
            for key, value in extra.items():
                self._pending[key] = value
        self._flush(force=True)

    def cancelled(self) -> bool:
        try:
            operation = self.store.get_operation(self.key)
        except Exception:
            return False
        return bool(operation and operation.get("cancel_requested"))

    def raise_if_cancelled(self, message: str) -> None:
        if self.cancelled():
            raise OperationCancelled(message)

    # -- internals -------------------------------------------------------------

    def _flush(self, *, force: bool) -> None:
        now = time.monotonic()
        with self._lock:
            if not self._pending:
                return
            if not force and (now - self._last_flush) < self.min_interval:
                return
            pending = self._pending
            self._pending = {}
            self._last_flush = now
        pending.setdefault("heartbeat_at", utcnow())
        try:
            self.store.operation_progress(self.key, **pending)
        except Exception as exc:  # pragma: no cover - telemetry is not authoritative
            log.warning("progress update failed for %s: %s", self.key, exc)


class NullReporter:
    """No-op reporter for synchronous/CLI callers."""

    def stage(self, *args, **kwargs) -> None:
        pass

    def progress(self, *args, **kwargs) -> None:
        pass

    def set_state(self, *args, **kwargs) -> None:
        pass

    def heartbeat(self) -> None:
        pass

    def finish(self, *args, **kwargs) -> None:
        pass

    def cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self, message: str) -> None:
        return


__all__ = ["NullReporter", "OperationCancelled", "ProgressReporter"]
