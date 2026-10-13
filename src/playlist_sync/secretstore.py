"""Managed secret storage for credentials created through the UI.

Only credentials that this application created are stored here. Externally
supplied credentials (``plex.token`` / ``plex.token_file``) always take
precedence and are never overwritten.

Storage is a single file per secret with mode ``0600`` inside a ``0700``
directory, written atomically (temp file + :func:`os.replace`) so a failed write
never destroys the previous working credential. This is **plaintext** storage on
the configured ``/config`` volume; we do not claim it is encrypted. Protect the
volume with host permissions and back it up accordingly.
"""

from __future__ import annotations

import os
from pathlib import Path

from .redact import register_secret

SECRETS_DIRNAME = "secrets"
PLEX_TOKEN_NAME = "plex_token"
GONIC_PASSWORD_NAME = "gonic_password"


def secrets_dir(config_dir: str | Path) -> Path:
    return Path(config_dir) / SECRETS_DIRNAME


def managed_secret_path(config_dir: str | Path, name: str) -> Path:
    return secrets_dir(config_dir) / name


def managed_token_path(config_dir: str | Path) -> Path:
    return managed_secret_path(config_dir, PLEX_TOKEN_NAME)


def read_managed_secret(config_dir: str | Path, name: str) -> str | None:
    path = managed_secret_path(config_dir, name)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if value:
        register_secret(value)
        return value
    return None


def write_managed_secret(config_dir: str | Path, name: str, value: str) -> bool:
    """Atomically write a managed secret with restrictive permissions."""
    if not value:
        return False
    directory = secrets_dir(config_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError:
        return False
    path = managed_secret_path(config_dir, name)
    temp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)  # atomic; previous credential survives any failure
        os.chmod(path, 0o600)
    except OSError:
        import contextlib

        with contextlib.suppress(OSError):
            temp.unlink()
        return False
    register_secret(value)
    return True


def delete_managed_secret(config_dir: str | Path, name: str) -> bool:
    path = managed_secret_path(config_dir, name)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def has_managed_secret(config_dir: str | Path, name: str) -> bool:
    return managed_secret_path(config_dir, name).exists()


def read_managed_token(config_dir: str | Path) -> str | None:
    return read_managed_secret(config_dir, PLEX_TOKEN_NAME)


def write_managed_token(config_dir: str | Path, token: str) -> bool:
    return write_managed_secret(config_dir, PLEX_TOKEN_NAME, token)


def delete_managed_token(config_dir: str | Path) -> bool:
    return delete_managed_secret(config_dir, PLEX_TOKEN_NAME)


def has_managed_token(config_dir: str | Path) -> bool:
    return has_managed_secret(config_dir, PLEX_TOKEN_NAME)
