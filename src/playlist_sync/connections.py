"""Editable connection settings: draft, read-only testing, atomic save.

Precedence: environment/YAML (external) always wins over UI-managed values.
Secrets use :mod:`playlist_sync.secretstore`; non-secret fields use
:mod:`playlist_sync.settings_store`. Saving validates the exact draft first,
persists atomically (previous settings survive any failure), and reports when a
server/identity change requires mapping revalidation.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .config import AppSettings, external_fields, validate_config
from .errors import AppError
from .settings_store import NON_SECRET_FIELDS, get_path, load_managed, save_managed, set_path
from .store.db import utcnow

MANAGED_SECRET_NAMES = {"plex.token": "plex_token", "gonic.password": "gonic_password"}


def _get_attr(settings: AppSettings, dotted: str) -> Any:
    section, attr = dotted.split(".", 1)
    return getattr(getattr(settings, section), attr)


def _set_attr(settings: AppSettings, dotted: str, value: Any) -> None:
    section, attr = dotted.split(".", 1)
    setattr(getattr(settings, section), attr, value)


def connection_state(settings: AppSettings) -> dict[str, Any]:
    external = external_fields()
    managed = load_managed(settings.config_dir)
    fields = {
        dotted: {"value": _get_attr(settings, dotted), "external": dotted in external}
        for dotted in NON_SECRET_FIELDS
    }
    plex_secret = settings.plex_secret()
    gonic_secret = settings.gonic_secret()
    return {
        "fields": fields,
        "plex": {
            "external": "plex.token" in external,
            "configured": bool(plex_secret),
            "source": settings.plex_credential_source(),
            "token_file": settings.plex.token_file,
        },
        "gonic": {
            "external": "gonic.password" in external,
            "configured": bool(gonic_secret),
            "source": settings.gonic_credential_source(),
        },
        "validated": managed.get("validated") or {},
        "updated_at": managed.get("updated_at"),
    }


def _parse_list(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _looks_masked(value: str) -> bool:
    stripped = (value or "").strip()
    return bool(stripped) and all(ch in "*•" for ch in stripped)


def build_draft(settings: AppSettings, form: Any) -> AppSettings:
    """Build a candidate settings object from a submitted form (no persistence)."""
    external = external_fields()
    draft = settings.model_copy(deep=True)

    if "plex.url" not in external and form.get("plex_url") is not None:
        draft.plex.url = str(form.get("plex_url")).strip() or draft.plex.url
    if "plex.library_sections" not in external and form.get("plex_library_sections") is not None:
        draft.plex.library_sections = _parse_list(str(form.get("plex_library_sections")))
    if "gonic.url" not in external and form.get("gonic_url") is not None:
        draft.gonic.url = str(form.get("gonic_url")).strip() or draft.gonic.url
    if "gonic.username" not in external and form.get("gonic_username") is not None:
        draft.gonic.username = str(form.get("gonic_username")).strip()
    if "gonic.music_folder_id" not in external and form.get("gonic_music_folder_id") is not None:
        value = str(form.get("gonic_music_folder_id")).strip()
        draft.gonic.music_folder_id = value or None

    # Secrets are replaced only on explicit request, and never for external fields.
    if form.get("replace_plex_token") == "on" and "plex.token" not in external:
        token = str(form.get("plex_token") or "").strip()
        if token and not _looks_masked(token):
            draft.plex.token = token
            draft.plex.token_file = None
    if form.get("replace_gonic_password") == "on" and "gonic.password" not in external:
        password = str(form.get("gonic_password") or "")
        if password and not _looks_masked(password):
            draft.gonic.password = password
    return draft


def test_connection(draft: AppSettings) -> dict[str, Any]:
    """Read-only connectivity/authentication test of a draft. Never mutates."""
    from .engine.context import build_gonic, build_plex
    from .matching.paths import PathMapper, PathRoot

    mapper = PathMapper(
        [PathRoot(name=r.name, plex_root=r.plex_root, gonic_root=r.gonic_root) for r in draft.sync.path_roots]
    )
    report: dict[str, Any] = {}
    for side, builder in (("plex", build_plex), ("gonic", build_gonic)):
        entry: dict[str, Any] = {"reachable": False, "authenticated": False, "errors": [], "warnings": []}
        try:
            client = builder(draft, mapper)
        except AppError as exc:
            entry["errors"].append(exc.message)
            report[side] = entry
            continue
        except Exception as exc:
            entry["errors"].append(f"could not connect ({type(exc).__name__})")
            report[side] = entry
            continue
        try:
            result = client.doctor()
            entry.update(
                reachable=result.reachable,
                authenticated=result.authenticated,
                server_version=result.server_version,
                open_subsonic=result.open_subsonic,
                features=result.features,
                warnings=result.warnings,
                errors=result.errors,
            )
            if side == "plex":
                entry["identity"] = str(getattr(client._server, "machineIdentifier", "") or "")
                entry["server_name"] = str(getattr(client._server, "friendlyName", "") or "")
                entry["music_sections"] = result.discovered.get("music_sections", [])
            else:
                entry["identity"] = draft.gonic.url
                entry["music_folders"] = result.discovered.get("music_folders", [])
            entry["url"] = draft.plex.url if side == "plex" else draft.gonic.url
        finally:
            client.close()
        report[side] = entry
    return report


@dataclass
class SaveResult:
    ok: bool
    message: str = ""
    errors: list[dict[str, str]] = field(default_factory=list)
    test: dict[str, Any] = field(default_factory=dict)
    needs_confirmation: bool = False
    confirmation_details: list[str] = field(default_factory=list)
    server_changed: bool = False
    changed_sides: list[str] = field(default_factory=list)
    saved_fields: list[str] = field(default_factory=list)


def _identity_changes(settings: AppSettings, draft: AppSettings) -> tuple[bool, list[str], list[str]]:
    managed = load_managed(settings.config_dir)
    validated = managed.get("validated") or {}
    details: list[str] = []
    sides: list[str] = []
    for side, cfg in (("plex", draft.plex), ("gonic", draft.gonic)):
        previous = validated.get(side) or {}
        previous_url = previous.get("url")
        if previous_url and previous_url != cfg.url:
            details.append(f"{side} URL changed from {previous_url} to {cfg.url}")
            sides.append(side)
    return bool(details), sides, details


def save_connection(
    settings: AppSettings,
    form: Any,
    *,
    confirm_change: bool = False,
) -> SaveResult:
    """Validate and persist a draft. Previous settings survive any failure."""
    external = external_fields()
    draft = build_draft(settings, form)

    errors = validate_config(draft, require_credentials=False)
    if errors:
        return SaveResult(ok=False, message="The connection settings are invalid.", errors=errors)

    test = test_connection(draft)
    changed, sides, details = _identity_changes(settings, draft)
    if changed and not confirm_change:
        return SaveResult(
            ok=False,
            message="This changes the server or account scope; confirmation is required.",
            test=test,
            needs_confirmation=True,
            confirmation_details=details,
        )

    # Only persist fields that are not externally managed.
    managed = load_managed(settings.config_dir)
    new_managed = copy.deepcopy(managed)
    saved_fields: list[str] = []
    for dotted in NON_SECRET_FIELDS:
        if dotted in external:
            continue
        _envvar, path = NON_SECRET_FIELDS[dotted]
        set_path(new_managed, path, _get_attr(draft, dotted))
        saved_fields.append(dotted)

    # Persist replacement secrets first (atomic each; keeps previous on failure).
    from .secretstore import write_managed_secret

    for dotted, name in MANAGED_SECRET_NAMES.items():
        if dotted in external:
            continue
        replacing = form.get(
            "replace_plex_token" if dotted == "plex.token" else "replace_gonic_password"
        ) == "on"
        if not replacing:
            continue
        value = str(
            form.get("plex_token") if dotted == "plex.token" else form.get("gonic_password") or ""
        ).strip()
        if dotted == "gonic.password":
            value = str(form.get("gonic_password") or "")
        if not value or _looks_masked(value):
            continue
        if not write_managed_secret(settings.config_dir, name, value):
            return SaveResult(
                ok=False,
                message="The new credential could not be saved; the previous credential is unchanged.",
                test=test,
            )
        saved_fields.append(dotted)

    new_managed["validated"] = dict(managed.get("validated") or {})
    for side, entry in test.items():
        if entry.get("authenticated"):
            new_managed["validated"][side] = {
                "url": entry.get("url"),
                "identity": entry.get("identity"),
                "when": utcnow(),
            }
    new_managed["updated_at"] = utcnow()
    if not save_managed(settings.config_dir, new_managed):
        return SaveResult(
            ok=False,
            message="The settings file could not be written; previous settings are unchanged.",
            test=test,
        )

    # Activate on the live settings object (caller resets clients/caches).
    for dotted in NON_SECRET_FIELDS:
        if dotted in external:
            continue
        _set_attr(settings, dotted, _get_attr(draft, dotted))

    return SaveResult(
        ok=True,
        message="Connection settings saved.",
        test=test,
        server_changed=changed,
        changed_sides=sides,
        saved_fields=saved_fields,
    )


def remove_secret(settings: AppSettings, dotted: str) -> bool:
    """Remove a UI-managed secret (never an external credential)."""
    external = external_fields()
    if dotted in external:
        return False
    name = MANAGED_SECRET_NAMES.get(dotted)
    if not name:
        return False
    from .secretstore import delete_managed_secret

    return delete_managed_secret(settings.config_dir, name)


def managed_extra(settings: AppSettings, key: str, default: Any = None) -> Any:
    return load_managed(settings.config_dir).get(key, default)


__all__ = [
    "SaveResult",
    "build_draft",
    "connection_state",
    "get_path",
    "remove_secret",
    "save_connection",
    "test_connection",
]
