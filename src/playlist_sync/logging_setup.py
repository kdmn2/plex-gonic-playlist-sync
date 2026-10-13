"""Logging configuration with defensive redaction."""

from __future__ import annotations

import logging
import re

from .redact import RedactingFilter

_QUERY = re.compile(r"\?[^\s\"']+")


class QueryStripFilter(logging.Filter):
    """Remove URL query strings (which may carry auth params) from log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        stripped = _QUERY.sub("?<redacted>", message)
        if stripped != message:
            record.msg = stripped
            record.args = ()
        return True


def configure_logging(level: str = "INFO", *, log_http: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if log_http else getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    handler.addFilter(RedactingFilter())
    handler.addFilter(QueryStripFilter())
    root.addHandler(handler)

    # Silence libraries that could log raw URLs/headers; we opt into redacted
    # debug output via our own hooks instead.
    for name in ("httpx", "httpcore", "urllib3", "plexapi"):
        logging.getLogger(name).setLevel(logging.WARNING)
