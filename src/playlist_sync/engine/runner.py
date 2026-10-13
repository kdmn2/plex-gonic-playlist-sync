"""Background execution of long operations with progress reporting."""

from __future__ import annotations

import logging
import threading
import uuid

from ..store.db import (
    LIVE_OPERATION_STATES,
    TERMINAL_OPERATION_STATES,
    OperationState,
    utcnow,
)
from .progress import OperationCancelled, ProgressReporter

log = logging.getLogger("playlist_sync.runner")


class BackgroundRunner:
    """Runs operation workers in daemon threads, one row per operation.

    The initiating HTTP request returns the operation id immediately; the worker
    updates the durable ``operations`` row (progress + authoritative state).
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self._threads: dict[str, threading.Thread] = {}

    def running(self) -> list[str]:
        return [key for key, thread in self._threads.items() if thread.is_alive()]

    def spawn(self, worker) -> None:
        """Run a worker that registers its own operation row (e.g. via the engine)."""
        thread = threading.Thread(target=self._spawn_run, args=(worker,), daemon=True)
        thread.start()

    def _spawn_run(self, worker) -> None:
        try:
            worker()
        except Exception:  # pragma: no cover - the worker records its own state
            log.exception("spawned operation failed")

    def start(self, *, kind: str, name: str, worker, submission_key: str | None = None, **fields) -> str:
        store = self.ctx.store
        key = submission_key or str(uuid.uuid4())
        existing = store.get_operation(key)
        if existing is not None:
            # Deduplicate: same submission (running, or already finished) -> reuse.
            log.info("operation %s already exists in state %s; not starting another", key, existing["state"])
            return key
        try:
            store.create_operation(
                operation_key=key,
                direction=kind,
                intended_name=name,
                state=OperationState.QUEUED,
                step="queued",
                message="Queued",
                **fields,
            )
        except Exception:
            # Durable registration failed: do not claim the operation started.
            log.exception("could not register operation %s", key)
            raise
        thread = threading.Thread(target=self._run, args=(key, worker), name=f"op-{key[:12]}", daemon=True)
        self._threads[key] = thread
        thread.start()
        return key

    def _run(self, key: str, worker) -> None:
        reporter = ProgressReporter(self.ctx.store, key)
        try:
            self.ctx.store.operation_progress(
                key,
                state=OperationState.RUNNING,
                started_at=utcnow(),
                stage="starting",
                stage_desc="Starting",
                message="Starting",
            )
            result = worker(reporter) or {}
        except OperationCancelled as exc:
            reporter.finish(OperationState.CANCELLED_SAFE, str(exc))
        except Exception as exc:
            log.exception("background operation %s failed", key)
            current = self.ctx.store.get_operation(key) or {}
            if current.get("state") not in TERMINAL_OPERATION_STATES:
                reporter.finish(
                    OperationState.FAILED,
                    f"{type(exc).__name__}: {exc}",
                    error_code=getattr(exc, "code", None),
                )
        else:
            if not result.get("finalized"):
                state = result.get("state") or OperationState.SUCCEEDED
                reporter.finish(state, result.get("message", ""))
        finally:
            self._threads.pop(key, None)


def is_live(operation: dict) -> bool:
    return operation.get("state") in LIVE_OPERATION_STATES


__all__ = ["BackgroundRunner", "is_live"]
