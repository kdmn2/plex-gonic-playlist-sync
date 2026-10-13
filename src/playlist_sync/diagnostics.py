"""Sanitised diagnostics export.

By default the export excludes playlist names, track metadata and file paths, and
never contains credentials. ``include_details=True`` opts into mapping/playlist
identifiers (still never secrets). Call :func:`preview` to show the user what an
export will contain before they download it.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .redact import scrub
from .store.db import utcnow


def preview() -> dict[str, Any]:
    return {
        "included_always": [
            "app version and generation time",
            "sanitised configuration (URLs, limits, flags; never secrets)",
            "state schema version, installation id, storage health",
            "backend capability results (reachable/authenticated/version/features)",
            "recent sanitised error codes and run statuses",
            "active operation states",
        ],
        "excluded_always": [
            "Plex token, Gonic password, session secret",
            "authorization headers, cookies, auth query strings",
        ],
        "included_with_opt_in": [
            "mapping ids and names",
            "playlist ids and names",
            "operation detail text",
        ],
    }


def _sanitized_config(settings) -> dict[str, Any]:
    return {
        "plex": {
            "url": settings.plex.url,
            "library_sections": settings.plex.library_sections,
            "verify_tls": settings.plex.verify_tls,
            "ca_bundle_set": bool(settings.plex.ca_bundle),
            "timeout": settings.plex.timeout,
            "token_set": bool(settings.plex.token),
        },
        "gonic": {
            "url": settings.gonic.url,
            "username": settings.gonic.username,
            "verify_tls": settings.gonic.verify_tls,
            "ca_bundle_set": bool(settings.gonic.ca_bundle),
            "timeout": settings.gonic.timeout,
            "use_token_auth": settings.gonic.use_token_auth,
            "password_set": bool(settings.gonic.password),
        },
        "sync": {
            "poll_seconds": settings.sync.poll_seconds,
            "duration_tolerance_ms": settings.sync.duration_tolerance_ms,
            "block_on_unmatched": settings.sync.block_on_unmatched,
            "allow_partial": settings.sync.allow_partial,
            "allow_clear_target_when_source_empty": settings.sync.allow_clear_target_when_source_empty,
            "path_roots": [
                {"name": r.name, "plex_root": r.plex_root, "gonic_root": r.gonic_root}
                for r in settings.sync.path_roots
            ],
        },
        "resilience": settings.resilience.model_dump(),
        "limits": settings.limits.model_dump(),
        "ui": {"enabled": settings.ui.enabled, "username": settings.ui.username},
    }


def _backend_checks(ctx) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for side in ("plex", "gonic"):
        try:
            report = ctx.backend(side).doctor()  # type: ignore[arg-type]
            checks[side] = {
                "reachable": report.reachable,
                "authenticated": report.authenticated,
                "server_version": report.server_version,
                "open_subsonic": report.open_subsonic,
                "features": report.features,
                "warnings": [scrub(w) for w in report.warnings],
                "errors": [scrub(e) for e in report.errors],
            }
        except Exception as exc:
            checks[side] = {"reachable": False, "error": scrub(f"{type(exc).__name__}: {exc}")}
    return checks


def _recent_errors(ctx, limit: int = 50) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        runs = ctx.store.list_runs(limit=200)
    except Exception:
        return out
    for run in runs:
        if not run.get("error") and run.get("status") in {"applied_verified", "no_change"}:
            continue
        out.append(
            {
                "run_id": run.get("id"),
                "mapping_id": run.get("mapping_id"),
                "status": run.get("status"),
                "error_code": run.get("error_code"),
                "error": scrub(run.get("error") or ""),
                "when": run.get("started_at"),
            }
        )
        if len(out) >= limit:
            break
    return out


def _operations(ctx) -> list[dict[str, Any]]:
    try:
        ops = ctx.store.list_active_operations()
    except Exception:
        return []
    return [
        {
            "operation_key": op.get("operation_key"),
            "mapping_id": op.get("mapping_id"),
            "state": op.get("state"),
            "step": op.get("step"),
            "error_code": op.get("error_code"),
            "detail": scrub(op.get("detail") or ""),
        }
        for op in ops
    ]


def build(ctx, *, include_details: bool = False) -> dict[str, Any]:
    export: dict[str, Any] = {
        "app": {"version": __version__, "generated_at": utcnow()},
        "storage": ctx.storage_health(),
        "config": _sanitized_config(ctx.settings),
        "recent_errors": _recent_errors(ctx),
        "active_operations": _operations(ctx),
        "include_details": include_details,
    }
    if ctx.has_store and ctx.storage_ready:
        export["installation_id"] = ctx.store.installation_id
        export["schema_version"] = ctx.store.schema_version
    if include_details and ctx.has_store:
        import contextlib

        with contextlib.suppress(Exception):
            export["mappings"] = [
                {
                    "id": m.id,
                    "name": m.name,
                    "direction": m.direction,
                    "paused": m.paused,
                    "plex_playlist_id": m.plex_playlist_id,
                    "gonic_playlist_id": m.gonic_playlist_id,
                }
                for m in ctx.store.list_mappings()
            ]
    try:
        export["backends"] = _backend_checks(ctx)
    except Exception as exc:
        export["backends"] = {"error": scrub(str(exc))}
    return export


def write(ctx, path: str, *, include_details: bool = False) -> str:
    import json
    from pathlib import Path

    data = build(ctx, include_details=include_details)
    target = Path(path)
    target.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return str(target)
