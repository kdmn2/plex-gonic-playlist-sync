"""UI-managed connection settings (non-secret).

Persists only non-secret fields (server URLs, usernames, library scope) to
``/config/ui-settings.json``, written atomically with ``0600``. Credentials are
never stored here; they use :mod:`playlist_sync.secretstore`.

Precedence is resolved in :mod:`playlist_sync.config`: environment/YAML values
(external) always win over these managed values, and the UI marks externally
managed fields read-only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MANAGED_SETTINGS_NAME = "ui-settings.json"
MANAGED_VERSION = 1

# dotted field -> (env var, yaml path tuple)
NON_SECRET_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "plex.url": ("PLAYLIST_SYNC_PLEX__URL", ("plex", "url")),
    "plex.library_sections": ("PLAYLIST_SYNC_PLEX__LIBRARY_SECTIONS", ("plex", "library_sections")),
    "gonic.url": ("PLAYLIST_SYNC_GONIC__URL", ("gonic", "url")),
    "gonic.username": ("PLAYLIST_SYNC_GONIC__USERNAME", ("gonic", "username")),
    "gonic.music_folder_id": ("PLAYLIST_SYNC_GONIC__MUSIC_FOLDER_ID", ("gonic", "music_folder_id")),
}

# dotted secret field -> (env var, yaml path tuple, secret name)
SECRET_FIELDS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "plex.token": ("PLAYLIST_SYNC_PLEX__TOKEN", ("plex", "token"), "plex_token"),
    "gonic.password": ("PLAYLIST_SYNC_GONIC__PASSWORD", ("gonic", "password"), "gonic_password"),
}


def managed_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / MANAGED_SETTINGS_NAME


def load_managed(config_dir: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(managed_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    data.setdefault("version", MANAGED_VERSION)
    return data


def save_managed(config_dir: str | Path, data: dict[str, Any]) -> bool:
    path = managed_path(config_dir)
    temp = path.with_name(path.name + ".tmp")
    payload = dict(data)
    payload["version"] = MANAGED_VERSION
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        return True
    except OSError:
        import contextlib

        with contextlib.suppress(OSError):
            temp.unlink()
        return False


def get_path(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = data
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value
