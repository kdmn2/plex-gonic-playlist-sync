"""UI session authentication and CSRF protection.

The UI is never write-capable without authentication: ``serve`` refuses to start
without a UI password. Sessions are signed with ``itsdangerous``; every mutation
POST carries a CSRF token bound to the session.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import AppSettings

SESSION_COOKIE = "pgs_session"
SESSION_MAX_AGE = 12 * 3600


class AuthManager:
    def __init__(self, settings: AppSettings):
        self.username = settings.ui.username
        secret = settings.ui.session_secret
        if not secret:
            # Derive a stable secret so sessions survive restarts without a
            # configured secret. Not ideal, but never empty.
            secret = hashlib.sha256(
                f"{settings.ui.username}:{settings.ui.password}".encode()
            ).hexdigest()
        self._serializer = URLSafeTimedSerializer(secret, salt="pgs-session")
        self._password = settings.ui.password

    @property
    def configured(self) -> bool:
        return bool(self._password)

    def verify_credentials(self, username: str, password: str) -> bool:
        if not self.configured:
            return False
        user_ok = hmac.compare_digest(username, self.username)
        pass_ok = hmac.compare_digest(password, self._password)
        return user_ok and pass_ok

    def new_session(self) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        token = self._serializer.dumps({"u": self.username, "csrf": csrf})
        return token, csrf

    def read(self, token: str | None) -> dict | None:
        if not token:
            return None
        try:
            return self._serializer.loads(token, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None
