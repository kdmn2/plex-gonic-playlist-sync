"""Plex PIN authorization and token validation.

Implements the documented PIN flow used by Plex clients (and by python-plexapi's
``MyPlexPinLogin``): ``POST https://plex.tv/api/v2/pins`` returns a short code
that the user enters at https://plex.tv/link; polling
``GET /api/v2/pins/{id}`` returns an ``authToken``.

Security properties:
- Server-side only. The token is never returned to the browser, never placed in
  a URL, and is registered with the redactor immediately.
- No password is collected, no browser cookies/credential store are read, and
  redirects are never followed (so a token is never forwarded to another origin).
- Bounded expiry and a minimum polling interval.
- The account token is a full Plex account token; it is not a scoped or
  least-privileged credential. Revoke it at plex.tv > Account > Authorized Devices.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from . import __version__
from .redact import register_secret

PLEX_TV_BASE = "https://plex.tv"
PLEX_LINK_URL = "https://plex.tv/link"
DEFAULT_TTL = 600.0
DEFAULT_POLL_INTERVAL = 2.0


def _headers(client_identifier: str) -> dict[str, str]:
    return {
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Product": "Plex-Gonic Playlist Sync",
        "X-Plex-Version": __version__,
        "X-Plex-Device": "Linux",
        "X-Plex-Device-Name": "Plex-Gonic Playlist Sync",
        "X-Plex-Platform": "Linux",
        "Accept": "application/json",
    }


def _parse(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    try:
        root = ET.fromstring(text)
        out: dict[str, Any] = dict(root.attrib)
        out["_tag"] = root.tag
        return out
    except ET.ParseError:
        return {}


@dataclass
class PinFlow:
    pin_id: str
    code: str
    created: float
    ttl: float
    poll_interval: float
    last_poll: float = 0.0
    token: str | None = field(default=None, repr=False)
    status: str = "pending"  # pending | authorized | expired | denied | cancelled

    def expired(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.created >= self.ttl

    @property
    def link_url(self) -> str:
        return PLEX_LINK_URL

    def public(self) -> dict[str, Any]:
        """Safe representation for the UI (never includes the token)."""
        return {
            "code": self.code,
            "link_url": self.link_url,
            "status": self.status,
            "expires_in": max(0, int(self.ttl - (time.monotonic() - self.created))),
        }


class PlexPinAuth:
    def __init__(
        self,
        client_identifier: str,
        *,
        base_url: str = PLEX_TV_BASE,
        timeout: float = 10.0,
        ttl: float = DEFAULT_TTL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        transport: httpx.BaseTransport | None = None,
    ):
        self.client_identifier = client_identifier
        self.ttl = ttl
        self.poll_interval = poll_interval
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=_headers(client_identifier),
            transport=transport,
            follow_redirects=False,
        )

    def create(self) -> PinFlow:
        response = self._client.post("/api/v2/pins")
        response.raise_for_status()
        data = _parse(response.text)
        pin_id = data.get("id")
        code = data.get("code")
        if not pin_id or not code:
            raise RuntimeError("Plex did not return a PIN")
        return PinFlow(
            pin_id=str(pin_id),
            code=str(code),
            created=time.monotonic(),
            ttl=self.ttl,
            poll_interval=self.poll_interval,
        )

    def poll(self, flow: PinFlow) -> str:
        """Poll once. Returns the flow status; sets ``flow.token`` when authorized."""
        now = time.monotonic()
        if flow.status in {"cancelled", "authorized"}:
            return flow.status
        if flow.expired(now):
            flow.status = "expired"
            return flow.status
        if now - flow.last_poll < flow.poll_interval:
            return flow.status
        flow.last_poll = now

        try:
            response = self._client.get(f"/api/v2/pins/{flow.pin_id}")
        except httpx.HTTPError:
            return flow.status  # transient; try again next poll
        if response.status_code == 404:
            flow.status = "expired"
            return flow.status
        if response.status_code in (401, 403):
            flow.status = "denied"
            return flow.status
        if response.status_code != 200:
            return flow.status
        data = _parse(response.text)
        token = data.get("authToken") or data.get("auth_token")
        if token:
            register_secret(str(token))
            flow.token = str(token)
            flow.status = "authorized"
        return flow.status

    def cancel(self, flow: PinFlow) -> None:
        import contextlib

        flow.status = "cancelled"
        flow.token = None
        with contextlib.suppress(httpx.HTTPError):
            self._client.delete(f"/api/v2/pins/{flow.pin_id}")

    def close(self) -> None:
        self._client.close()


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    server_name: str | None = None
    server_identity: str | None = None
    server_version: str | None = None
    music_sections: list[dict[str, str]] = field(default_factory=list)
    # True when the configured URL redirected; we refuse rather than forward the token.
    redirect_blocked: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "server_name": self.server_name,
            "server_identity": self.server_identity,
            "server_version": self.server_version,
            "music_sections": self.music_sections,
            "redirect_blocked": self.redirect_blocked,
        }


def validate_token(
    base_url: str,
    token: str,
    *,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> ValidationResult:
    """Validate a candidate token against the *configured* Plex server.

    Read-only: fetches the server identity and library sections. Never mutates
    playlists. Never follows redirects, so the token cannot be forwarded to a
    different origin.
    """
    if not token:
        return ValidationResult(False, "no token provided")
    parsed = urlparse(base_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ValidationResult(False, "the configured Plex URL is not valid")

    register_secret(token)
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    client = httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        headers=headers,
        transport=transport,
        follow_redirects=False,
    )
    try:
        root = client.get("/")
        if 300 <= root.status_code < 400:
            return ValidationResult(
                False, "the Plex URL redirected; refusing to forward the token", redirect_blocked=True
            )
        if root.status_code in (401, 403):
            return ValidationResult(False, "Plex rejected the token")
        if root.status_code != 200:
            return ValidationResult(False, f"Plex returned HTTP {root.status_code}")
        container = _parse(root.text).get("MediaContainer", {})
        result = ValidationResult(
            ok=True,
            server_name=container.get("friendlyName"),
            server_identity=container.get("machineIdentifier"),
            server_version=container.get("version"),
        )

        sections = client.get("/library/sections")
        if 300 <= sections.status_code < 400:
            return ValidationResult(
                False, "the Plex URL redirected; refusing to forward the token", redirect_blocked=True
            )
        if sections.status_code in (401, 403):
            return ValidationResult(False, "the token cannot read the library sections")
        if sections.status_code == 200:
            directories = _parse(sections.text).get("MediaContainer", {}).get("Directory", [])
            for directory in directories:
                if isinstance(directory, dict) and directory.get("type") == "artist":
                    result.music_sections.append(
                        {
                            "key": str(directory.get("key", "")),
                            "title": str(directory.get("title", "")),
                        }
                    )
        return result
    except httpx.HTTPError as exc:
        return ValidationResult(False, f"could not reach the configured Plex server ({type(exc).__name__})")
    finally:
        client.close()


def mask_token(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


def looks_like_token(value: str) -> bool:
    """Plex tokens are 20-ish alphanumeric chars; reject obvious junk early."""
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{16,64}", value or ""))
