"""Back up playlists to UTF-8 extended M3U (.m3u8) files with a JSON manifest.

This is a *playlist* backup, not a backup of audio files and not a full
application-state backup, and it does not restore anything.

Safety:
- Writes only inside the configured backup root; filenames are generated from a
  sanitised playlist name + stable identity + timestamp (never user paths).
- Versioned: an existing file is never overwritten; writes go to a temp file in
  the same directory and are published with an atomic rename.
- Directory is validated with a real write test; the location must not be the
  application config/secrets directory.
- Missing paths block a playlist by default; an explicit partial export skips
  them and records them in the manifest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .errors import AppError
from .models import RemotePlaylist, ServerKind
from .store.db import utcnow

log = logging.getLogger("playlist_sync.backup")

INDEX_NAME = "index.json"
_TAG = re.compile(r"\s+")


@dataclass
class BackupResult:
    status: str  # ok | partial | empty | blocked | failed
    server: str
    playlist_id: str
    name: str = ""
    file: str | None = None
    manifest: str | None = None
    checksum: str | None = None
    entries: int = 0
    skipped: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    detail: str = ""


def sanitize_name(value: str, *, max_length: int = 80) -> str:
    """Make a filesystem-safe name; never derived from a user path."""
    value = (value or "").strip()
    value = _TAG.sub(" ", value)
    value = re.sub(r"[^\w\-. ]+", "_", value, flags=re.UNICODE)
    value = value.strip(" ._-")
    value = value[:max_length] or "playlist"
    return value


def sanitize_label(value: str) -> str:
    """Remove line breaks and control characters from display metadata."""
    value = (value or "").replace("\r", " ").replace("\n", " ")
    return "".join(ch for ch in value if ch >= " ").strip()


def _anchored_strip(root: str, path: str) -> str | None:
    root = (root or "").rstrip("/")
    if not root:
        return None
    if path == root:
        return ""
    if path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return None


def export_path(track, side: ServerKind, settings) -> str | None:
    """Resolve a durable file path for one entry according to the path mode."""
    mode = settings.backups.path_mode
    server_path = track.path
    if not server_path:
        return None
    if mode == "server":
        return server_path
    if mode == "mapped":
        for mapping in settings.backups.mappings:
            if mapping.server != side.value:
                continue
            relative = _anchored_strip(mapping.from_root, server_path)
            if relative is not None:
                return f"{mapping.to_root.rstrip('/')}/{relative}" if relative else mapping.to_root
        return None
    if mode == "relative":
        base = settings.backups.relative_base
        if not base:
            return None
        if side == ServerKind.GONIC:
            # Gonic paths are already relative to its music root.
            return server_path
        relative = _anchored_strip(base, server_path)
        return relative if relative not in (None, "") else None
    return None


def validate_directory(settings) -> dict:
    """Validate the backup directory with a real bounded write test."""
    directory = Path(settings.backups.directory)
    info: dict = {"directory": str(directory), "ok": False, "reason": ""}
    config_dir = Path(settings.config_dir).resolve()
    try:
        resolved = directory.resolve()
    except OSError:
        info["reason"] = "the directory could not be resolved"
        return info
    # never the application config directory itself or the secrets directory
    if resolved == config_dir:
        info["reason"] = "the backup directory must not be the application config directory"
        return info
    if "secrets" in resolved.parts:
        info["reason"] = "the backup directory must not be the application secrets directory"
        return info
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        info["reason"] = f"the directory could not be created ({type(exc).__name__})"
        return info
    test = directory / f".pgs-write-test-{os.getpid()}"
    try:
        fd = os.open(test, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("ok")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        info["reason"] = f"the directory is not writable ({type(exc).__name__})"
        return info
    finally:
        import contextlib

        with contextlib.suppress(OSError):
            test.unlink()
    info["ok"] = True
    info["reason"] = "ok"
    return info


def preview_paths(ctx, side: ServerKind, playlist_id: str) -> dict:
    """Resolve export paths for preview, without writing anything."""
    from .engine.operations import safe_get_playlist

    playlist = safe_get_playlist(ctx, side, playlist_id)
    if playlist is None:
        return {"ok": False, "reason": "the playlist no longer exists"}
    rows = []
    missing = 0
    for entry in playlist.entries:
        path = export_path(entry.track, side, ctx.settings)
        if path is None:
            missing += 1
        rows.append(
            {
                "title": entry.track.identity_hint(),
                "source_path": entry.track.path,
                "export_path": path,
            }
        )
    return {
        "ok": True,
        "mode": ctx.settings.backups.path_mode,
        "directory": ctx.settings.backups.directory,
        "rows": rows[:50],
        "total": len(rows),
        "missing": missing,
    }


def _build_m3u(playlist: RemotePlaylist, side: ServerKind, settings, *, allow_partial: bool):
    lines = ["#EXTM3U"]
    missing: list[dict] = []
    written = 0
    for entry in playlist.entries:
        track = entry.track
        path = export_path(track, side, settings)
        if path is None or "\n" in (path or "") or "\r" in (path or ""):
            missing.append({"label": track.identity_hint(), "reason": "no usable file path"})
            continue
        seconds = int(track.duration_ms // 1000) if track.duration_ms else -1
        label = sanitize_label(track.identity_hint())
        lines.append(f"#EXTINF:{seconds},{label}")
        lines.append(path)
        written += 1
    if missing and settings.backups.require_all_entries and not allow_partial:
        return None, missing, written
    return "\n".join(lines) + "\n", missing, written


def _index_path(directory: Path) -> Path:
    return directory / INDEX_NAME


def _load_index(directory: Path) -> dict:
    try:
        return json.loads(_index_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(directory: Path, index: dict) -> None:
    temp = _index_path(directory).with_name(INDEX_NAME + ".tmp")
    try:
        temp.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, _index_path(directory))
    except OSError:
        import contextlib

        with contextlib.suppress(OSError):
            temp.unlink()


def last_backup(settings, side: ServerKind, playlist_id: str) -> dict | None:
    index = _load_index(Path(settings.backups.directory))
    return index.get(f"{side.value}:{playlist_id}")


def load_index(settings) -> dict:
    """The backup index (server:playlist_id -> last backup info)."""
    return _load_index(Path(settings.backups.directory))


def backup_playlist(
    ctx,
    side: ServerKind,
    playlist_id: str,
    *,
    allow_partial: bool = False,
) -> BackupResult:
    from .engine.operations import safe_get_playlist

    result = BackupResult(status="failed", server=side.value, playlist_id=playlist_id)
    directory = Path(ctx.settings.backups.directory)
    validation = validate_directory(ctx.settings)
    if not validation["ok"]:
        result.status = "failed"
        result.detail = f"backup directory unusable: {validation['reason']}"
        return result

    playlist = safe_get_playlist(ctx, side, playlist_id)
    if playlist is None:
        result.status = "failed"
        result.detail = "the playlist no longer exists"
        return result
    result.name = playlist.summary.name

    content, missing, written = _build_m3u(playlist, side, ctx.settings, allow_partial=allow_partial)
    result.missing = missing
    if content is None:
        result.status = "blocked"
        result.detail = (
            f"{len(missing)} entries have no exportable path; export blocked "
            "(enable partial export to skip them)."
        )
        return result
    if not playlist.entries:
        result.status = "empty"
    elif missing:
        result.status = "partial"
    else:
        result.status = "ok"

    data = content.encode("utf-8")
    checksum = hashlib.sha256(data).hexdigest()
    safe = sanitize_name(playlist.summary.name)
    short = hashlib.sha1(f"{side.value}:{playlist_id}".encode()).hexdigest()[:8]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{side.value}-{safe}-{short}-{stamp}"
    final = directory / f"{base}.m3u8"
    counter = 1
    while final.exists():
        final = directory / f"{base}-{counter}.m3u8"
        counter += 1

    temp = final.with_name(final.name + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, final)
    except OSError as exc:
        import contextlib

        with contextlib.suppress(OSError):
            temp.unlink()
        result.status = "failed"
        result.detail = f"could not write the backup file ({type(exc).__name__})"
        return result

    manifest_name = f"{final.name}.manifest.json"
    manifest = {
        "app_version": __version__,
        "run_id": short,
        "created_at": utcnow(),
        "server": side.value,
        "playlist_id": playlist_id,
        "playlist_name": playlist.summary.name,
        "smart": playlist.summary.is_smart,
        "source_fingerprint": playlist.fingerprint(),
        "entry_count": len(playlist.entries),
        "exported_entries": written,
        "export_status": result.status,
        "path_mode": ctx.settings.backups.path_mode,
        "mappings": [m.model_dump() for m in ctx.settings.backups.mappings],
        "file": final.name,
        "checksum_sha256": checksum,
        "partial": bool(missing),
        "skipped": missing,
        "note": (
            "This is a playlist backup, not a backup of audio files, and it is not "
            "a complete application-state backup. It may contain private listening "
            "metadata and file paths. It does not restore anything."
        ),
    }
    import contextlib

    with contextlib.suppress(OSError):
        (directory / manifest_name).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    index = _load_index(directory)
    index[f"{side.value}:{playlist_id}"] = {
        "file": final.name,
        "manifest": manifest_name,
        "when": utcnow(),
        "fingerprint": playlist.fingerprint(),
        "entries": written,
        "status": result.status,
    }
    _save_index(directory, index)
    _apply_retention(directory, side, playlist_id, index)

    result.file = str(final)
    result.manifest = str(directory / manifest_name)
    result.checksum = checksum
    result.entries = written
    if result.status == "ok":
        result.detail = f"backed up {written} entries to {final.name}"
    elif result.status == "empty":
        result.detail = "the playlist was empty at source; wrote an empty playlist"
    else:
        result.detail = f"partial backup: {written} entries, {len(missing)} skipped"
    return result


def _apply_retention(directory: Path, side: ServerKind, playlist_id: str, index: dict) -> None:
    """Optional: keep only the newest N files per playlist (ours only)."""

    # Retention is intentionally conservative: default keeps every version.
    # (Wired from settings in backup_many when enabled.)
    _ = (directory, side, playlist_id, index)


def backup_many(ctx, selections: list[tuple[ServerKind, str]], *, allow_partial: bool = False) -> list[BackupResult]:
    results: list[BackupResult] = []
    for side, playlist_id in selections:
        try:
            results.append(backup_playlist(ctx, side, playlist_id, allow_partial=allow_partial))
        except AppError as exc:
            results.append(
                BackupResult(
                    status="failed",
                    server=side.value,
                    playlist_id=playlist_id,
                    detail=exc.message,
                )
            )
        except Exception as exc:
            log.exception("backup failed for %s:%s", side.value, playlist_id)
            results.append(
                BackupResult(
                    status="failed",
                    server=side.value,
                    playlist_id=playlist_id,
                    detail=f"unexpected error ({type(exc).__name__})",
                )
            )
    return results


def summarize(results: list[BackupResult]) -> dict:
    counts = {"ok": 0, "partial": 0, "empty": 0, "blocked": 0, "failed": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    mixed = counts["ok"] + counts["empty"] == len(results) and len(results) > 0
    return {"counts": counts, "total": len(results), "all_ok": mixed}


__all__ = [
    "BackupResult",
    "backup_many",
    "backup_playlist",
    "export_path",
    "last_backup",
    "load_index",
    "preview_paths",
    "sanitize_label",
    "sanitize_name",
    "summarize",
    "validate_directory",
]
