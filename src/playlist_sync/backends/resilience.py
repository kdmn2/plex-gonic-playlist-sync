"""Resilience primitives: bounded retry, jitter, Retry-After, circuit breaking.

These are transport-agnostic. Backends use them to:
- retry only safe (idempotent read) operations,
- honour a valid ``Retry-After`` within a configured upper bound,
- stop hammering an unhealthy backend (circuit breaker),
- log an outage once instead of flooding the log, and log recovery once.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass

from ..errors import AppError

log = logging.getLogger("playlist_sync.resilience")


@dataclass
class RetryPolicy:
    attempts: int = 3
    base: float = 1.0
    factor: float = 2.0
    max_delay: float = 30.0
    total: float = 60.0
    respect_retry_after: bool = True
    retry_after_max: float = 120.0

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Delay before retry number ``attempt`` (1-based). Includes jitter."""
        if retry_after is not None and self.respect_retry_after:
            return max(0.0, min(retry_after, self.retry_after_max))
        raw = min(self.base * (self.factor ** (attempt - 1)), self.max_delay)
        return raw + random.uniform(0, raw * 0.25)


class CircuitOpen(AppError):
    def __init__(self, name: str, seconds_remaining: float):
        super().__init__(
            code="BACKEND_UNAVAILABLE",
            message=f"{name} is temporarily unavailable; requests are being suppressed.",
            details={"circuit": name, "seconds_remaining": round(seconds_remaining, 1)},
        )


class CircuitBreaker:
    """Per-backend circuit breaker with a half-open probe."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 60.0,
        clock=time.monotonic,
    ):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.reset_seconds = max(1.0, reset_seconds)
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._clock() - self._opened_at >= self.reset_seconds:
                return "half_open"
            return "open"

    def before_call(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            elapsed = self._clock() - self._opened_at
            if elapsed < self.reset_seconds:
                raise CircuitOpen(self.name, self.reset_seconds - elapsed)
            # half-open: allow a single probe through
            return

    def record_success(self) -> None:
        with self._lock:
            if self._opened_at is not None:
                log.info("%s backend reachable again (circuit closed)", self.name)
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold and self._opened_at is None:
                self._opened_at = self._clock()
                log.warning(
                    "%s backend marked unavailable after %d failures; suppressing requests for %.0fs",
                    self.name,
                    self._failures,
                    self.reset_seconds,
                )


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    # HTTP-date form
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
        if when is None:
            return None
        import datetime as _dt

        now = _dt.datetime.now(_dt.UTC)
        return max(0.0, (when - now).total_seconds())
    except (TypeError, ValueError):
        return None


def is_retryable(error: AppError) -> bool:
    from ..errors import Retryability

    return error.retryability is Retryability.TRANSIENT


class OutageLogger:
    """Logs the first occurrence of an outage, then suppresses repeats."""

    def __init__(self, name: str, window_seconds: float = 60.0):
        self.name = name
        self.window = window_seconds
        self._last: dict[str, float] = {}
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def outage(self, code: str, message: str) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last.get(code, 0.0)
            first = code not in self._active
            self._active.add(code)
            if first or now - last >= self.window:
                self._last[code] = now
                log.warning("%s: %s (%s)", self.name, message, code)

    def recovered(self) -> None:
        with self._lock:
            if self._active:
                log.info("%s: connection recovered", self.name)
            self._active.clear()
            self._last.clear()


def with_retry(
    call,
    policy: RetryPolicy,
    *,
    breaker: CircuitBreaker | None = None,
    outages: OutageLogger | None = None,
    idempotent: bool = True,
):
    """Execute ``call`` with bounded retry for safe operations.

    Non-idempotent operations (mutations) are never retried here; the caller must
    reconcile first. Raises the last AppError if all attempts fail.
    """
    if not idempotent or policy.attempts <= 1:
        return _once(call, breaker, outages)

    deadline = time.monotonic() + policy.total
    attempt = 0
    last: AppError | None = None
    while attempt < policy.attempts:
        attempt += 1
        try:
            result = _once(call, breaker, outages)
            if outages is not None:
                outages.recovered()
            return result
        except AppError as exc:
            last = exc
            if not is_retryable(exc) or attempt >= policy.attempts:
                raise
            delay = policy.delay_for(attempt, exc.retry_after)
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
    assert last is not None
    raise last


def _once(call, breaker: CircuitBreaker | None, outages: OutageLogger | None):
    if breaker is not None:
        breaker.before_call()
    try:
        result = call()
        if breaker is not None:
            breaker.record_success()
        return result
    except AppError as exc:
        if breaker is not None and exc.retryability.value in {"transient", "after_reconcile"}:
            breaker.record_failure()
        if outages is not None:
            outages.outage(exc.code, exc.message)
        raise
