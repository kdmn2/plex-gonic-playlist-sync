"""Cross-process state-directory lock.

Prevents concurrent scheduled/manual/CLI syncs from sharing the same state
directory. This is an advisory file lock in the data directory; it is separate
from the SQLite row-based lock (which handles same-process coordination).
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType


class StateDirectoryBusy(RuntimeError):
    """Raised when another process already holds the state lock."""


class StateLock:
    def __init__(self, data_dir: str | Path, name: str = "sync.lock"):
        self.path = Path(data_dir) / name
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise StateDirectoryBusy(
                f"another sync is already running (lock held on {self.path})"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> StateLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
