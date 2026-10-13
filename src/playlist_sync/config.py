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
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .errors import make_error
from .redact import Secret, register_secret

ENV_PREFIX = "PLAYLIST_SYNC_"


class PlexSettings(BaseModel):
    url: str = "http://plex:32400"
    token: str = ""
    # Optional path to a file containing the token (external, read-only).
    token_file: str | None = None
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
    jobs_poll_seconds: int = 30
    path_roots: list[PathRootSettings] = Field(default_factory=list)


class ResilienceSettings(BaseModel):
    """Bounded retry and circuit-breaking policy for backend calls."""

    retry_attempts: int = 3
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    retry_total_seconds: float = 60.0
    respect_retry_after: bool = True
    retry_after_max_seconds: float = 120.0
    circuit_failure_threshold: int = 5
    circuit_reset_seconds: float = 60.0


class LimitsSettings(BaseModel):
    """Resource and input limits (see docs/error-handling.md)."""

    max_response_bytes: int = 64 * 1024 * 1024
    max_playlist_entries: int = 100_000
    max_pages: int = 10_000
    max_page_repeats: int = 3
    max_run_seconds: int = 1800
    max_concurrent_tasks: int = 4
    min_free_bytes: int = 64 * 1024 * 1024
    runs_retention: int = 500
    snapshots_retention: int = 200
    index_timeout_seconds: float = 30.0
    warm_indexes: bool = True
    index_refresh_seconds: int = 900


class LoggingSettings(BaseModel):
    directory: str | None = None  # defaults to <data_dir>/logs
    max_bytes: int = 5 * 1024 * 1024
    backups: int = 3
    dedupe_window_seconds: float = 60.0


class BackupMappingSettings(BaseModel):
    server: str  # plex | gonic
    from_root: str
    to_root: str


class BackupsSettings(BaseModel):
    enabled: bool = True
    directory: str = "/backups"
    path_mode: str = "server"  # server | mapped | relative
    relative_base: str | None = None
    mappings: list[BackupMappingSettings] = Field(default_factory=list)
    require_all_entries: bool = True
    retention: int = 0  # 0 = keep every version


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
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    backups: BackupsSettings = Field(default_factory=BackupsSettings)
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
        # managed credentials (read registers them with the redactor)
        from .secretstore import read_managed_secret

        read_managed_secret(self.config_dir, "plex_token")
        read_managed_secret(self.config_dir, "gonic_password")

    def plex_secret(self) -> Secret:
        """Resolve the Plex token with explicit precedence.

        External configuration (``plex.token``) wins, then an external token
        file (``plex.token_file``), then a credential created through the UI
        (stored under ``/config/secrets``). External credentials are never
        overwritten by the UI.
        """
        if self.plex.token:
            return Secret(self.plex.token)
        if self.plex.token_file:
            from pathlib import Path

            try:
                value = Path(self.plex.token_file).read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if value:
                return Secret(value)
        from .secretstore import read_managed_token

        managed = read_managed_token(self.config_dir)
        if managed:
            return Secret(managed)
        return Secret("")

    @property
    def plex_external_configured(self) -> bool:
        return bool(self.plex.token or self.plex.token_file)

    def plex_credential_source(self) -> str:
        if self.plex.token:
            return "environment/config"
        if self.plex.token_file:
            return "token_file"
        from .secretstore import has_managed_token

        if has_managed_token(self.config_dir):
            return "ui-managed"
        return "none"

    def gonic_secret(self) -> Secret:
        if self.gonic.password:
            return Secret(self.gonic.password)
        from .secretstore import read_managed_secret

        managed = read_managed_secret(self.config_dir, "gonic_password")
        return Secret(managed or "")

    def gonic_credential_source(self) -> str:
        if self.gonic.password:
            return "environment/config"
        from .secretstore import has_managed_secret

        if has_managed_secret(self.config_dir, "gonic_password"):
            return "ui-managed"
        return "none"

    def apply_managed(self) -> None:
        """Overlay UI-managed non-secret settings for non-external fields."""
        from .settings_store import NON_SECRET_FIELDS, get_path, load_managed

        managed = load_managed(self.config_dir)
        external = external_fields()
        for dotted, (_envvar, path) in NON_SECRET_FIELDS.items():
            if dotted in external:
                continue
            value = get_path(managed, path)
            if value is None:
                continue
            if dotted == "sync.path_roots":
                from .settings_store import coerce_path_roots

                value = coerce_path_roots(value)
            if dotted == "backups.mappings":
                from .config import BackupMappingSettings

                value = [
                    BackupMappingSettings(**m) if isinstance(m, dict) else m for m in (value or [])
                ]
            section, attr = dotted.split(".", 1)
            setattr(getattr(self, section), attr, value)

    def verify_for(self, side: str) -> bool | str:
        cfg = self.plex if side == "plex" else self.gonic
        if cfg.ca_bundle:
            return cfg.ca_bundle
        return cfg.verify_tls


def yaml_config() -> dict[str, Any]:
    """The raw YAML configuration dict (empty if none)."""
    path = _yaml_path()
    if not path or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _yaml_has(data: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def external_fields() -> set[str]:
    """Dotted field names supplied externally (environment or YAML)."""
    from .settings_store import NON_SECRET_FIELDS, SECRET_FIELDS

    env = os.environ
    data = yaml_config()
    out: set[str] = set()
    for dotted, (envvar, path) in NON_SECRET_FIELDS.items():
        if envvar in env or _yaml_has(data, path):
            out.add(dotted)
    for dotted, (envvar, path, _name) in SECRET_FIELDS.items():
        if envvar in env or _yaml_has(data, path):
            out.add(dotted)
    # A configured token file is an external source for the Plex token.
    if "PLAYLIST_SYNC_PLEX__TOKEN_FILE" in env or _yaml_has(data, ("plex", "token_file")):
        out.add("plex.token")
    if "PLAYLIST_SYNC_PLEX__TOKEN" in env or _yaml_has(data, ("plex", "token")):
        out.add("plex.token")
    return out


def materialize_secret_files(environ: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Turn ``*_FILE`` variables into their direct counterparts.

    Returns a list of ``{field, message}`` errors for unreadable secret files so
    configuration validation can report them precisely.
    """
    env = environ if environ is not None else os.environ
    errors: list[dict[str, str]] = []
    for key in list(env.keys()):
        if not key.startswith(ENV_PREFIX) or not key.endswith("_FILE"):
            continue
        target = key[: -len("_FILE")]
        path = Path(env[key])
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            errors.append(
                {"field": target.lower(), "message": f"secret file is not readable: {path}"}
            )
            continue
        if not value:
            errors.append({"field": target.lower(), "message": f"secret file is empty: {path}"})
            continue
        env.setdefault(target, value)
    return errors


def _url_error(field: str, value: str) -> dict[str, str] | None:
    parsed = urlparse(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"field": field, "message": "must be a valid http(s) URL, e.g. http://host:port"}
    return None


def _overlaps(a: str, b: str) -> bool:
    a = a.rstrip("/")
    b = b.rstrip("/")
    return a == b or b.startswith(a + "/") or a.startswith(b + "/")


def validate_config(
    settings: AppSettings,
    *,
    require_credentials: bool = False,
    secret_file_errors: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Field-level configuration validation. Returns a list of errors (empty = ok)."""
    errors: list[dict[str, str]] = list(secret_file_errors or [])

    for field, value in (("plex.url", settings.plex.url), ("gonic.url", settings.gonic.url)):
        if err := _url_error(field, value):
            errors.append(err)
    if settings.plex.token_file:
        from pathlib import Path

        token_path = Path(settings.plex.token_file)
        if not token_path.is_file():
            errors.append(
                {"field": "plex.token_file", "message": "is not a readable regular file"}
            )
        else:
            try:
                if not token_path.read_text(encoding="utf-8").strip():
                    errors.append({"field": "plex.token_file", "message": "is empty"})
            except OSError:
                errors.append({"field": "plex.token_file", "message": "could not be read"})
    if settings.plex.timeout <= 0:
        errors.append({"field": "plex.timeout", "message": "must be greater than 0"})
    if settings.gonic.timeout <= 0:
        errors.append({"field": "gonic.timeout", "message": "must be greater than 0"})

    if settings.sync.poll_seconds < 0:
        errors.append({"field": "sync.poll_seconds", "message": "must be 0 or greater"})
    if not 0 <= settings.sync.duration_tolerance_ms <= 60_000:
        errors.append(
            {"field": "sync.duration_tolerance_ms", "message": "must be between 0 and 60000"}
        )
    if settings.sync.append_chunk_size < 1:
        errors.append({"field": "sync.append_chunk_size", "message": "must be at least 1"})

    res = settings.resilience
    if res.retry_attempts < 0:
        errors.append({"field": "resilience.retry_attempts", "message": "must be 0 or greater"})
    if res.retry_base_seconds <= 0:
        errors.append({"field": "resilience.retry_base_seconds", "message": "must be greater than 0"})
    if res.retry_max_seconds < res.retry_base_seconds:
        errors.append(
            {"field": "resilience.retry_max_seconds", "message": "must be >= retry_base_seconds"}
        )
    if res.retry_total_seconds <= 0:
        errors.append({"field": "resilience.retry_total_seconds", "message": "must be greater than 0"})
    if res.circuit_failure_threshold < 1:
        errors.append(
            {"field": "resilience.circuit_failure_threshold", "message": "must be at least 1"}
        )
    if res.circuit_reset_seconds <= 0:
        errors.append({"field": "resilience.circuit_reset_seconds", "message": "must be greater than 0"})

    lim = settings.limits
    for field, value in (
        ("limits.max_response_bytes", lim.max_response_bytes),
        ("limits.max_playlist_entries", lim.max_playlist_entries),
        ("limits.max_pages", lim.max_pages),
        ("limits.max_page_repeats", lim.max_page_repeats),
        ("limits.max_run_seconds", lim.max_run_seconds),
        ("limits.max_concurrent_tasks", lim.max_concurrent_tasks),
        ("limits.runs_retention", lim.runs_retention),
        ("limits.snapshots_retention", lim.snapshots_retention),
    ):
        if value < 1:
            errors.append({"field": field, "message": "must be at least 1"})
    if lim.min_free_bytes < 0:
        errors.append({"field": "limits.min_free_bytes", "message": "must be 0 or greater"})

    # Path roots: names unique, pairs unique, no overlapping/conflicting roots.
    seen_names: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    plex_to_gonic: dict[str, str] = {}
    roots = list(settings.sync.path_roots)
    for i, root in enumerate(roots):
        prefix = f"sync.path_roots[{i}]"
        if not root.name.strip():
            errors.append({"field": f"{prefix}.name", "message": "must not be empty"})
        elif root.name in seen_names:
            errors.append({"field": f"{prefix}.name", "message": f"duplicate name {root.name!r}"})
        seen_names.add(root.name)
        if not root.plex_root.strip() or not root.gonic_root.strip():
            errors.append({"field": prefix, "message": "plex_root and gonic_root are required"})
            continue
        if not root.plex_root.startswith("/") or not root.gonic_root.startswith("/"):
            errors.append({"field": prefix, "message": "roots should be absolute paths"})
        pair = (root.plex_root.rstrip("/"), root.gonic_root.rstrip("/"))
        if pair in seen_pairs:
            errors.append({"field": prefix, "message": "duplicate path-root mapping"})
        seen_pairs.add(pair)
        existing = plex_to_gonic.get(pair[0])
        if existing is not None and existing != pair[1]:
            errors.append(
                {
                    "field": prefix,
                    "message": f"plex_root {pair[0]!r} maps to multiple gonic roots",
                }
            )
        plex_to_gonic[pair[0]] = pair[1]
    for i in range(len(roots)):
        for j in range(i + 1, len(roots)):
            if _overlaps(roots[i].plex_root, roots[j].plex_root):
                errors.append(
                    {
                        "field": f"sync.path_roots[{j}].plex_root",
                        "message": f"overlaps sync.path_roots[{i}].plex_root",
                    }
                )
            if _overlaps(roots[i].gonic_root, roots[j].gonic_root):
                errors.append(
                    {
                        "field": f"sync.path_roots[{j}].gonic_root",
                        "message": f"overlaps sync.path_roots[{i}].gonic_root",
                    }
                )

    ui_err = (
        _url_error("ui.base_url", settings.ui.base_url)
        if settings.ui.enabled and settings.ui.base_url
        else None
    )
    if ui_err:
        errors.append(ui_err)

    if require_credentials:
        if not settings.plex.token:
            errors.append({"field": "plex.token", "message": "is required"})
        if not settings.gonic.username:
            errors.append({"field": "gonic.username", "message": "is required"})
        if not settings.gonic.password:
            errors.append({"field": "gonic.password", "message": "is required"})
    return errors


def load_settings(config_path: str | Path | None = None) -> AppSettings:
    if config_path:
        os.environ.setdefault(f"{ENV_PREFIX}CONFIG_FILE", str(config_path))
    secret_errors = materialize_secret_files()
    settings = AppSettings()
    settings.apply_managed()
    settings.register_secrets()
    errors = validate_config(settings, secret_file_errors=secret_errors)
    if errors:
        err = make_error("CONFIG_INVALID", "The configuration is invalid.")
        err.details["fields"] = errors
        raise err
    return settings


def build_path_mapper(settings: AppSettings):
    from .matching.paths import PathMapper, PathRoot

    roots = [
        PathRoot(name=r.name, plex_root=r.plex_root, gonic_root=r.gonic_root)
        for r in settings.sync.path_roots
    ]
    return PathMapper(roots)
