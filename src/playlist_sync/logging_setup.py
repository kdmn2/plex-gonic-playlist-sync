"""Logging configuration with defensive redaction and structured context.

- Console handler (human readable) and an optional rotating file handler.
- Every record can carry structured context: ``error_code``, ``run_id``,
  ``mapping_id``, ``backend``, ``attempt``; missing values default to ``-``.
- Secret values and URL query strings are scrubbed from every record.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .redact import RedactingFilter

_QUERY = re.compile(r"\?[^\s\"']+")
_CONTEXT_FIELDS = ("error_code", "run_id", "mapping_id", "backend", "attempt", "operation_id")


class QueryStripFilter(logging.Filter):
    """Remove URL query strings (which may carry auth params) from log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        stripped = _QUERY.sub("?<redacted>", message)
        if stripped != message:
            record.msg = stripped
            record.args = ()
        return True


class ContextDefaultsFilter(logging.Filter):
    """Ensure structured context fields exist so the formatter never fails."""

    def filter(self, record: logging.LogRecord) -> bool:
        for field in _CONTEXT_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, "-")
        return True


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = " ".join(
            f"{field}={getattr(record, field)}"
            for field in _CONTEXT_FIELDS
            if getattr(record, field, "-") not in ("-", None)
        )
        return f"{base} {ctx}".rstrip()


def configure_logging(
    level: str = "INFO",
    *,
    log_http: bool = False,
    log_dir: str | Path | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 3,
) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if log_http else getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redactor = RedactingFilter()
    query_strip = QueryStripFilter()
    context = ContextDefaultsFilter()

    console = logging.StreamHandler()
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    for flt in (redactor, query_strip, context):
        console.addFilter(flt)
    root.addHandler(console)

    if log_dir:
        try:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                directory / "app.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
            file_handler.setFormatter(
                StructuredFormatter(
                    "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S"
                )
            )
            for flt in (redactor, query_strip, context):
                file_handler.addFilter(flt)
            root.addHandler(file_handler)
        except OSError:
            root.warning("could not open log file in %s; continuing with console logging", log_dir)

    # Silence libraries that could log raw URLs/headers; we opt into redacted
    # debug output ourselves.
    for name in ("httpx", "httpcore", "urllib3", "plexapi"):
        logging.getLogger(name).setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    error_code: str | None = None,
    run_id: int | None = None,
    mapping_id: str | None = None,
    backend: str | None = None,
    attempt: int | None = None,
    operation_id: str | None = None,
    **extra: Any,
) -> None:
    logger.log(
        level,
        message,
        extra={
            "error_code": error_code or "-",
            "run_id": run_id if run_id is not None else "-",
            "mapping_id": mapping_id or "-",
            "backend": backend or "-",
            "attempt": attempt if attempt is not None else "-",
            "operation_id": operation_id or "-",
            **extra,
        },
    )
