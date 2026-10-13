"""Read-only discovery helpers used by the CLI, doctor, and UI."""

from __future__ import annotations

from ..backends import CapabilityReport
from ..config import AppSettings, build_path_mapper
from ..models import ServerKind
from .context import EngineContext, build_gonic, build_plex


def discovery_playlists(ctx: EngineContext, side: ServerKind, *, cache: bool = True):
    backend = ctx.backend(side)
    playlists = backend.list_playlists(audio_only=True)
    if cache:
        ctx.store.replace_playlist_cache(side.value, ctx.server_identity(side), playlists)
    return playlists


def _unreachable(kind: str, exc: Exception) -> CapabilityReport:
    return CapabilityReport(kind=kind, reachable=False, authenticated=False, errors=[str(exc)])


def run_doctor(settings: AppSettings) -> dict:
    """Probe both backends. Never raises; failures are reported."""
    report: dict = {"plex": None, "gonic": None, "warnings": []}
    mapper = build_path_mapper(settings)
    for kind, builder in (("plex", build_plex), ("gonic", build_gonic)):
        try:
            client = builder(settings, mapper)
        except Exception as exc:
            report[kind] = _as_dict(_unreachable(kind, exc))
            continue
        try:
            report[kind] = _as_dict(client.doctor())
        except Exception as exc:
            report[kind] = _as_dict(_unreachable(kind, exc))
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                client.close()

    if not mapper.configured:
        report["warnings"].append(
            "no sync.path_roots configured; file-path matching is disabled and only "
            "MusicBrainz/metadata matching will apply"
        )
    return report


def _as_dict(report: CapabilityReport) -> dict:
    return {
        "kind": report.kind,
        "reachable": report.reachable,
        "authenticated": report.authenticated,
        "detail": report.detail,
        "server_version": report.server_version,
        "open_subsonic": report.open_subsonic,
        "features": report.features,
        "warnings": report.warnings,
        "errors": report.errors,
        "discovered": report.discovered,
    }
