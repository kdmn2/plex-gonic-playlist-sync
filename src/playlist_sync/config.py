"""Validated configuration.

Layering (highest priority first):
  1. explicit ``init`` kwargs
  2. environment variables (``PLAYLIST_SYNC_`` prefix, ``__`` nesting)
  3. YAML file (``PLAYLIST_SYNC_CONFIG_FILE`` or ``/config/config.yaml``)
  4. field defaults

Any secret field also accepts a ``*_FILE`` companion variable, e.g.
``PLAYLIST_SYNC_PLEX__TOKEN_FILE``, whose contents are used as the value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .redact import Secret, register_secret

ENV_PREFIX = "PLAYLIST_SYNC_"


class PlexSettings(BaseModel):
    url: str = "http://plex:32400"
    token: str = ""
    verify_tls: bool = True
    ca_bundle: str | None = None
    timeout: float = 30.0
    library_sections: list[str] = Field(default_factory=list)


class GonicSettings(BaseModel):
    url: str = "http://gonic:4747"
    username: str = ""
    password: str = ""
    verify_tls: bool = True
    ca_bundle: str | None = None
    timeout: float = 30.0
    music_folder_id: str | None = None
    use_token_auth: bool = True


class PathRootSettings(BaseModel):
    name: str = "default"
    plex_root: str
    gonic_root: str


class SyncSettings(BaseModel):
    poll_seconds: int = 300
    duration_tolerance_ms: int = 2000
    block_on_unmatched: bool = True
    allow_partial: bool = False
    delete_target_playlist: bool = False
    allow_clear_target_when_source_empty: bool = False
    append_chunk_size: int = 1000
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    path_roots: list[PathRootSettings] = Field(default_factory=list)


class UISettings(BaseModel):
    enabled: bool = True
    username: str = "admin"
    password: str = ""
    session_secret: str = ""
    base_url: str = "http://localhost:8080"


def _yaml_path() -> Path | None:
    value = os.environ.get(f"{ENV_PREFIX}CONFIG_FILE")
    if value:
        return Path(value)
    default = Path("/config/config.yaml")
    if default.exists():
        return default
    return None


class _YamlSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = _yaml_path()
        if not path or not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return {}
        return data or {}


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    data_dir: Path = Path("/data")
    config_dir: Path = Path("/config")
    log_level: str = "INFO"
    log_http: bool = False

    plex: PlexSettings = Field(default_factory=PlexSettings)
    gonic: GonicSettings = Field(default_factory=GonicSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    ui: UISettings = Field(default_factory=UISettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _YamlSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    # -- derived helpers -------------------------------------------------------

    def register_secrets(self) -> None:
        for value in (
            self.plex.token,
            self.gonic.password,
            self.ui.password,
            self.ui.session_secret,
        ):
            register_secret(value)

    def plex_secret(self) -> Secret:
        return Secret(self.plex.token)

    def gonic_secret(self) -> Secret:
        return Secret(self.gonic.password)

    def verify_for(self, side: str) -> bool | str:
        cfg = self.plex if side == "plex" else self.gonic
        if cfg.ca_bundle:
            return cfg.ca_bundle
        return cfg.verify_tls


def materialize_secret_files(environ: dict[str, str] | None = None) -> None:
    """Turn ``*_FILE`` variables into their direct counterparts."""
    env = environ if environ is not None else os.environ
    for key in list(env.keys()):
        if not key.startswith(ENV_PREFIX) or not key.endswith("_FILE"):
            continue
        target = key[: -len("_FILE")]
        path = Path(env[key])
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        env.setdefault(target, value)


def load_settings(config_path: str | Path | None = None) -> AppSettings:
    if config_path:
        os.environ.setdefault(f"{ENV_PREFIX}CONFIG_FILE", str(config_path))
    materialize_secret_files()
    settings = AppSettings()
    settings.register_secrets()
    return settings


def build_path_mapper(settings: AppSettings):
    from .matching.paths import PathMapper, PathRoot

    roots = [
        PathRoot(name=r.name, plex_root=r.plex_root, gonic_root=r.gonic_root)
        for r in settings.sync.path_roots
    ]
    return PathMapper(roots)
