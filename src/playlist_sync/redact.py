"""Secret handling and redaction.

Secrets are wrapped so that their ``repr``/``str`` never disclose the value.
A process-wide redactor scrubs known secret values from log records, HTTP debug
output, and error messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any


class Secret:
    """Wraps a sensitive string. Never prints its contents."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = value or ""

    def get(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return "***" if self._value else ""

    def __repr__(self) -> str:
        return "Secret('***')" if self._value else "Secret('')"


@dataclass
class _Redactor:
    secrets: set[str]

    def scrub(self, text: str) -> str:
        for value in self.secrets:
            if value and len(value) >= 4:
                text = text.replace(value, "***")
        return text


_REDACTOR = _Redactor(set())


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        _REDACTOR.secrets.add(value)


def scrub(text: str) -> str:
    return _REDACTOR.scrub(text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        scrubbed = scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def redact_mapping(data: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in keys:
            out[key] = "***"
        else:
            out[key] = value
    return out


SENSITIVE_KEYS = {"token", "password", "secret", "secret_key", "p", "t", "s", "authorization"}
