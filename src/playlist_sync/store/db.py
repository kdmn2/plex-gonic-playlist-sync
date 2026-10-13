"""SQLite-backed state: mappings, snapshots, baselines, runs, operations, locks.

Durability and integrity
------------------------
- A ``meta`` table holds the installation identity and schema version.
- ``user_version`` tracks the schema; migrations back up the database first and
  fail closed on an unsupported (newer) schema.
- A separate identity marker (normally ``/config/installation.json``) lets an
  existing deployment detect that its ``/data`` mount disappeared and refuse to
  silently start with a fresh empty database.
- Writes are transactional with a bounded busy timeout; storage errors are
  translated into typed :class:`~playlist_sync.errors.AppError`.

Integrity problems do not prevent opening the database: instead they set
``state_error`` and disable mutations, so diagnostics and recovery stay usable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import AppError, make_error

log = logging.getLogger("playlist_sync.store")

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000


class OperationState:
    PLANNED = "PLANNED"
    PREPARED = "PREPARED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    APPLYING = "APPLYING"
    VERIFYING = "VERIFYING"
    WAITING = "WAITING"
    RETRYING = "RETRYING"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_SAFE = "FAILED_SAFE"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
    CONFLICT = "CONFLICT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    CANCELLED_SAFE = "CANCELLED_SAFE"


# States that mean "not safely finished" and therefore need reconciliation.
ACTIVE_OPERATION_STATES = {
    OperationState.PLANNED,
    OperationState.PREPARED,
    OperationState.QUEUED,
    OperationState.RUNNING,
    OperationState.APPLYING,
    OperationState.VERIFYING,
    OperationState.WAITING,
    OperationState.RETRYING,
    OperationState.CANCELLING,
    OperationState.OUTCOME_UNKNOWN,
    OperationState.PARTIALLY_APPLIED,
    OperationState.RECOVERY_REQUIRED,
}
TERMINAL_OPERATION_STATES = {
    OperationState.SUCCEEDED,
    OperationState.FAILED,
    OperationState.FAILED_SAFE,
    OperationState.CANCELLED_SAFE,
    OperationState.CONFLICT,
}

# States in which a background worker is expected to be actively heartbeating.
LIVE_OPERATION_STATES = {
    OperationState.QUEUED,
    OperationState.RUNNING,
    OperationState.WAITING,
    OperationState.RETRYING,
    OperationState.CANCELLING,
}
HEARTBEAT_STALE_SECONDS = 120


def _operation_snapshot(operation: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw operations row for status API/UI consumption."""
    counters = operation.get("counters")
    if isinstance(counters, str) and counters:
        try:
            counters = json.loads(counters)
        except ValueError:
            counters = {}
    elif not isinstance(counters, dict):
        counters = {}
    out = dict(operation)
    out["counters"] = counters
    out["revision"] = operation.get("revision") or 0
    out["finished"] = operation.get("state") in TERMINAL_OPERATION_STATES
    out["cancel_available"] = (
        operation.get("state") in LIVE_OPERATION_STATES and not operation.get("cancel_requested")
    )
    return out

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    friendly_name TEXT,
    url TEXT,
    updated_at TEXT,
    UNIQUE(kind, identity)
);
CREATE TABLE IF NOT EXISTS playlists (
    server_kind TEXT NOT NULL,
    server_identity TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    name TEXT,
    owner TEXT,
    track_count INTEGER,
    is_smart INTEGER,
    is_audio INTEGER,
    writable INTEGER,
    updated_at_remote TEXT,
    last_seen_at TEXT,
    extra TEXT,
    PRIMARY KEY (server_kind, server_identity, playlist_id)
);
CREATE TABLE IF NOT EXISTS mappings (
    id TEXT PRIMARY KEY,
    name TEXT,
    direction TEXT NOT NULL DEFAULT 'plex_to_gonic',
    enabled INTEGER NOT NULL DEFAULT 1,
    paused INTEGER NOT NULL DEFAULT 0,
    baseline_mode TEXT NOT NULL DEFAULT 'manual_review',
    baseline_ready INTEGER NOT NULL DEFAULT 0,
    allow_partial INTEGER NOT NULL DEFAULT 0,
    plex_playlist_id TEXT,
    plex_name TEXT,
    gonic_playlist_id TEXT,
    gonic_name TEXT,
    created_at TEXT,
    updated_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS overrides (
    mapping_id TEXT NOT NULL,
    source_side TEXT NOT NULL,
    source_track_id TEXT NOT NULL,
    target_track_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT,
    PRIMARY KEY (mapping_id, source_side, source_track_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id TEXT,
    side TEXT NOT NULL,
    playlist_id TEXT,
    kind TEXT NOT NULL,
    entries TEXT NOT NULL,
    track_ids TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT,
    run_id INTEGER
);
CREATE TABLE IF NOT EXISTS baselines (
    mapping_id TEXT PRIMARY KEY,
    plex_fingerprint TEXT,
    gonic_fingerprint TEXT,
    plex_entries TEXT,
    gonic_entries TEXT,
    canonical TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id TEXT,
    mode TEXT,
    trigger TEXT,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    stats TEXT,
    error TEXT,
    error_code TEXT,
    verified INTEGER
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    mapping_id TEXT,
    side TEXT,
    operation TEXT,
    payload TEXT,
    status TEXT,
    created_at TEXT,
    completed_at TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_key TEXT UNIQUE,
    mapping_id TEXT,
    run_id INTEGER,
    direction TEXT,
    source_side TEXT,
    target_side TEXT,
    source_playlist_id TEXT,
    target_playlist_id TEXT,
    intended_name TEXT,
    state TEXT NOT NULL,
    step TEXT,
    source_fingerprint TEXT,
    target_fingerprint_before TEXT,
    desired_fingerprint TEXT,
    target_fingerprint_after TEXT,
    desired_ids TEXT,
    snapshot_id INTEGER,
    created_playlist_id TEXT,
    attempt INTEGER DEFAULT 0,
    error_code TEXT,
    detail TEXT,
    stage TEXT,
    stage_desc TEXT,
    completed INTEGER,
    total INTEGER,
    unit TEXT,
    counters TEXT,
    message TEXT,
    heartbeat_at TEXT,
    cancel_requested INTEGER DEFAULT 0,
    revision INTEGER DEFAULT 0,
    started_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id TEXT,
    run_id INTEGER,
    plex_entries TEXT,
    gonic_entries TEXT,
    resolution TEXT,
    resolved INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    owner TEXT,
    acquired_at TEXT,
    heartbeat_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT NOT NULL,
    params TEXT,
    frequency TEXT,
    timezone TEXT,
    run_mode TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    paused INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT,
    last_success TEXT,
    next_run TEXT,
    last_occurrence TEXT,
    last_result TEXT,
    config_revision INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state);
CREATE INDEX IF NOT EXISTS idx_operations_mapping ON operations(mapping_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_mapping ON snapshots(mapping_id);
CREATE INDEX IF NOT EXISTS idx_runs_mapping ON runs(mapping_id);
CREATE INDEX IF NOT EXISTS idx_jobs_next_run ON jobs(next_run);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _translate_sqlite(exc: sqlite3.Error) -> AppError:
    text = str(exc).lower()
    if "malformed" in text or "not a database" in text or "corrupt" in text:
        err = make_error("STORAGE_CORRUPT", "The state database appears to be corrupt.")
    elif "full" in text:
        err = make_error("STORAGE_FULL", "The state store ran out of space or inodes.")
    elif "readonly" in text or "read-only" in text:
        err = make_error("STORAGE_READ_ONLY", "The state store is read-only.")
    elif "locked" in text or "busy" in text:
        err = make_error("STATE_LOCK_TIMEOUT", "The state database is busy.")
    elif "no such table" in text or "no such column" in text:
        err = make_error("STATE_MIGRATION_FAILED", "The state schema is incomplete.")
    elif "unable to open database" in text:
        err = make_error("STORAGE_UNAVAILABLE", "The state database could not be opened for writing.")
    elif "disk i/o error" in text:
        err = make_error("STORAGE_UNAVAILABLE", "The state database had a disk I/O error.")
    else:
        err = make_error("STORAGE_UNAVAILABLE", "The state store could not be written.")
    # Keep the real SQLite message for diagnostics (it holds no user secrets).
    err.details["technical"] = str(exc)
    return err


@dataclass
class Mapping:
    id: str
    name: str
    direction: str
    enabled: bool
    paused: bool
    baseline_mode: str
    baseline_ready: bool
    allow_partial: bool
    plex_playlist_id: str | None
    plex_name: str | None
    gonic_playlist_id: str | None
    gonic_name: str | None
    note: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Mapping:
        return cls(
            id=row["id"],
            name=row["name"],
            direction=row["direction"],
            enabled=bool(row["enabled"]),
            paused=bool(row["paused"]),
            baseline_mode=row["baseline_mode"],
            baseline_ready=bool(row["baseline_ready"]),
            allow_partial=bool(row["allow_partial"]),
            plex_playlist_id=row["plex_playlist_id"],
            plex_name=row["plex_name"],
            gonic_playlist_id=row["gonic_playlist_id"],
            gonic_name=row["gonic_name"],
            note=row["note"],
        )


class Store:
    def __init__(
        self,
        path: str | Path,
        identity_file: str | Path | None = None,
        *,
        init_new: bool = False,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ):
        self.path = Path(path)
        self.identity_file = Path(identity_file) if identity_file else None
        self.busy_timeout_ms = busy_timeout_ms
        self.state_error: AppError | None = None
        self.mutations_allowed = True
        self.installation_id: str | None = None
        self._initialized_new = False

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise make_error("STORAGE_UNAVAILABLE", "The state directory could not be created.") from exc
        if not os.access(self.path.parent, os.W_OK):
            raise make_error("STORAGE_READ_ONLY", "The state directory is not writable.")

        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=busy_timeout_ms / 1000)
        except sqlite3.OperationalError as exc:
            raise _translate_sqlite(exc) from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._apply_pragmas()
        except sqlite3.Error as exc:
            translated = _translate_sqlite(exc)
            if translated.code == "STORAGE_CORRUPT":
                # Unreadable/corrupted file: record it and keep going in a
                # degraded, diagnostics-only mode rather than crashing startup.
                self.state_error = translated
                self.mutations_allowed = False
                return
            raise translated from exc
        log.info("state store opened: %s (journal_mode=%s)", self.path, self.journal_mode)
        try:
            self._migrate(init_new=init_new)
            self._ensure_columns()
            self._establish_identity(init_new=init_new)
            self._check_integrity()
        except AppError:
            raise
        except sqlite3.Error as exc:
            raise _translate_sqlite(exc) from exc

    # -- setup -----------------------------------------------------------------

    def _apply_pragmas(self) -> None:
        # Durability appropriate for persistent local storage. WAL is preferred
        # but not supported on every filesystem (e.g. some FUSE/network mounts);
        # fall back to a rollback journal rather than failing.
        self.journal_mode = "unknown"
        try:
            row = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            self.journal_mode = str(row[0]) if row else "wal"
        except sqlite3.Error as exc:
            log.warning("WAL journal mode unavailable (%s); falling back to DELETE", exc)
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self.journal_mode = "delete"
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")

    @property
    def schema_version(self) -> int:
        try:
            row = self._conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return -1

    def _tables_exist(self) -> bool:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mappings'"
        )
        return cur.fetchone() is not None

    def _backup(self, from_version: int) -> Path | None:
        if not self.path.exists():
            return None
        suffix = f".bak-v{from_version}-{int(time.time())}"
        backup = self.path.with_name(self.path.name + suffix)
        try:
            shutil.copy2(self.path, backup)
            for extra in ("-wal", "-shm"):
                side = Path(str(self.path) + extra)
                if side.exists():
                    shutil.copy2(side, Path(str(backup) + extra))
        except OSError:
            return None
        return backup

    def _migrate(self, *, init_new: bool) -> None:
        version = self.schema_version
        if version > SCHEMA_VERSION:
            self.state_error = make_error(
                "STATE_SCHEMA_UNSUPPORTED",
                "The state database was written by a newer version of the app.",
            )
            self.mutations_allowed = False
            return
        if version == SCHEMA_VERSION:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            return
        # version < SCHEMA_VERSION (0 means fresh or pre-versioned)
        had_tables = self._tables_exist()
        if version == 0 and had_tables:
            header = "migrating legacy state schema"
        else:
            header = "initialising state schema"
        if version != 0 or had_tables:
            self._backup(version)
        try:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()
        except sqlite3.Error as exc:
            self.state_error = make_error(
                "STATE_MIGRATION_FAILED", f"State migration failed ({header})."
            )
            self.state_error.details["technical"] = str(exc)
            self.mutations_allowed = False
        if version == 0 and not had_tables:
            self._initialized_new = True

    def _ensure_columns(self) -> None:
        """Additive, idempotent column migration for existing databases.

        Uses ``ALTER TABLE ADD COLUMN`` for columns missing from older state, so
        schema additions do not silently fail on an existing database.
        """
        desired = {
            "stage": "TEXT",
            "stage_desc": "TEXT",
            "completed": "INTEGER",
            "total": "INTEGER",
            "unit": "TEXT",
            "counters": "TEXT",
            "message": "TEXT",
            "heartbeat_at": "TEXT",
            "cancel_requested": "INTEGER DEFAULT 0",
            "revision": "INTEGER DEFAULT 0",
            "started_at": "TEXT",
        }
        try:
            rows = self._conn.execute("PRAGMA table_info(operations)").fetchall()
            have = {row[1] for row in rows}
            for column, ctype in desired.items():
                if column not in have:
                    self._conn.execute(f"ALTER TABLE operations ADD COLUMN {column} {ctype}")
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("could not ensure operations columns: %s", exc)

    def _counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("mappings", "runs", "operations", "snapshots", "baselines"):
            try:
                row = self._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                out[table] = int(row["c"]) if row else 0
            except sqlite3.Error:
                out[table] = 0
        return out

    def _read_identity_marker(self) -> dict[str, Any] | None:
        if self.identity_file is None or not self.identity_file.exists():
            return None
        try:
            return json.loads(self.identity_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_identity_marker(self, installation_id: str) -> None:
        if self.identity_file is None:
            return
        try:
            self.identity_file.parent.mkdir(parents=True, exist_ok=True)
            self.identity_file.write_text(
                json.dumps(
                    {"installation_id": installation_id, "schema_version": SCHEMA_VERSION},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            self.state_error = self.state_error or make_error(
                "STORAGE_READ_ONLY", "The configuration directory is not writable."
            )

    def _establish_identity(self, *, init_new: bool) -> None:
        marker = self._read_identity_marker()
        marker_id = (marker or {}).get("installation_id")
        db_id = self.meta_get("installation_id")
        counts = self._counts()
        has_data = any(counts.values())

        if init_new:
            new_id = str(uuid.uuid4())
            self.meta_set("installation_id", new_id)
            self._write_identity_marker(new_id)
            self.installation_id = new_id
            return

        if marker_id and not db_id and not has_data:
            # A marker exists but the data mount is empty: refuse to silently
            # start fresh. (Explicit recovery passes init_new=True.)
            self.state_error = make_error(
                "STATE_MISSING_MOUNT",
                "An existing installation's state appears empty; /data may not be mounted.",
            )
            self.mutations_allowed = False
            self.installation_id = marker_id
            return

        if marker_id and db_id and marker_id != db_id:
            self.state_error = make_error(
                "STATE_IDENTITY_MISMATCH",
                "The /data state does not belong to this installation.",
            )
            self.mutations_allowed = False
            self.installation_id = db_id
            return

        resolved = db_id or marker_id
        if not resolved:
            resolved = str(uuid.uuid4())
        if db_id is None:
            self.meta_set("installation_id", resolved)
        if marker_id is None:
            self._write_identity_marker(resolved)
        self.installation_id = resolved

    def _check_integrity(self) -> None:
        try:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
            ok = bool(row and str(row[0]).lower() == "ok")
        except sqlite3.Error:
            ok = False
        if not ok:
            self.state_error = self.state_error or make_error(
                "STORAGE_CORRUPT", "The state database failed its integrity check."
            )
            self.mutations_allowed = False

    # -- low level -------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self):
        with self._lock:
            try:
                cur = self._conn.cursor()
            except sqlite3.Error as exc:
                log.warning("state store cursor failed: %s", exc)
                raise _translate_sqlite(exc) from exc
            try:
                yield cur
                self._conn.commit()
            except sqlite3.Error as exc:
                import contextlib

                log.warning("state store operation failed: %s", exc)
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
                raise _translate_sqlite(exc) from exc
            finally:
                cur.close()

    def require_mutable(self) -> None:
        if self.state_error is not None:
            raise self.state_error
        if not self.mutations_allowed:
            raise make_error("STORAGE_UNAVAILABLE", "State is not available for mutations.")

    # -- meta ------------------------------------------------------------------

    def meta_get(self, key: str) -> str | None:
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None
        except sqlite3.Error:
            return None

    def meta_set(self, key: str, value: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # -- servers ---------------------------------------------------------------

    def upsert_server(self, kind: str, identity: str, friendly_name: str, url: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO servers (kind, identity, friendly_name, url, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(kind, identity) DO UPDATE SET
                     friendly_name=excluded.friendly_name, url=excluded.url,
                     updated_at=excluded.updated_at""",
                (kind, identity, friendly_name, url, utcnow()),
            )
            cur.execute("SELECT id FROM servers WHERE kind=? AND identity=?", (kind, identity))
            return int(cur.fetchone()["id"])

    # -- playlist cache (disposable) -------------------------------------------

    def replace_playlist_cache(self, kind: str, identity: str, playlists: Iterable[Any]) -> None:
        now = utcnow()
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM playlists WHERE server_kind=? AND server_identity=?", (kind, identity)
            )
            for pl in playlists:
                cur.execute(
                    """INSERT INTO playlists
                       (server_kind, server_identity, playlist_id, name, owner, track_count,
                        is_smart, is_audio, writable, updated_at_remote, last_seen_at, extra)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        kind,
                        identity,
                        pl.playlist_id,
                        pl.name,
                        pl.owner,
                        pl.track_count,
                        int(pl.is_smart),
                        int(pl.is_audio),
                        int(pl.writable) if pl.writable is not None else None,
                        pl.updated_at,
                        now,
                        json.dumps(pl.extra or {}),
                    ),
                )

    def cached_playlists(self, kind: str, identity: str) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM playlists WHERE server_kind=? AND server_identity=? ORDER BY name",
                (kind, identity),
            )
            return [dict(row) for row in cur.fetchall()]

    def clear_caches(self) -> int:
        """Rebuildable cache only; never touches mappings/snapshots/operations."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM playlists")
            return int(cur.rowcount)

    # -- mappings --------------------------------------------------------------

    def create_mapping(self, **fields: Any) -> Mapping:
        mid = fields.pop("id", None) or str(uuid.uuid4())
        now = utcnow()
        values = {
            "id": mid,
            "name": fields.get("name") or "mapping",
            "direction": fields.get("direction", "plex_to_gonic"),
            "enabled": int(bool(fields.get("enabled", True))),
            "paused": int(bool(fields.get("paused", False))),
            "baseline_mode": fields.get("baseline_mode", "manual_review"),
            "baseline_ready": int(bool(fields.get("baseline_ready", False))),
            "allow_partial": int(bool(fields.get("allow_partial", False))),
            "plex_playlist_id": fields.get("plex_playlist_id"),
            "plex_name": fields.get("plex_name"),
            "gonic_playlist_id": fields.get("gonic_playlist_id"),
            "gonic_name": fields.get("gonic_name"),
            "created_at": now,
            "updated_at": now,
            "note": fields.get("note"),
        }
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO mappings (id, name, direction, enabled, paused, baseline_mode,
                        baseline_ready, allow_partial, plex_playlist_id, plex_name,
                        gonic_playlist_id, gonic_name, created_at, updated_at, note)
                   VALUES (:id,:name,:direction,:enabled,:paused,:baseline_mode,:baseline_ready,
                        :allow_partial,:plex_playlist_id,:plex_name,:gonic_playlist_id,
                        :gonic_name,:created_at,:updated_at,:note)""",
                values,
            )
        return self.get_mapping(mid)  # type: ignore[return-value]

    def get_mapping(self, mapping_id: str) -> Mapping | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,))
            row = cur.fetchone()
            return Mapping.from_row(row) if row else None

    def list_mappings(self) -> list[Mapping]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM mappings ORDER BY name")
            return [Mapping.from_row(row) for row in cur.fetchall()]

    def update_mapping(self, mapping_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "name",
            "direction",
            "enabled",
            "paused",
            "baseline_mode",
            "baseline_ready",
            "allow_partial",
            "plex_playlist_id",
            "plex_name",
            "gonic_playlist_id",
            "gonic_name",
            "note",
        }
        sets = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"enabled", "paused", "baseline_ready", "allow_partial"}:
                value = int(bool(value))
            sets.append(f"{key}=?")
            values.append(value)
        sets.append("updated_at=?")
        values.append(utcnow())
        values.append(mapping_id)
        with self._cursor() as cur:
            cur.execute(f"UPDATE mappings SET {', '.join(sets)} WHERE id=?", values)

    def delete_mapping(self, mapping_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM mappings WHERE id=?", (mapping_id,))
            cur.execute("DELETE FROM baselines WHERE mapping_id=?", (mapping_id,))
            cur.execute("DELETE FROM overrides WHERE mapping_id=?", (mapping_id,))

    def has_unresolved_operations(self, mapping_id: str) -> bool:
        with self._cursor() as cur:
            placeholders = ",".join("?" for _ in ACTIVE_OPERATION_STATES)
            cur.execute(
                f"SELECT COUNT(*) AS c FROM operations WHERE mapping_id=? AND state IN ({placeholders})",
                (mapping_id, *ACTIVE_OPERATION_STATES),
            )
            row = cur.fetchone()
            return bool(row and row["c"])

    # -- overrides -------------------------------------------------------------

    def set_override(
        self, mapping_id: str, source_side: str, source_track_id: str, target_track_id: str, reason: str = ""
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO overrides (mapping_id, source_side, source_track_id,
                        target_track_id, reason, created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(mapping_id, source_side, source_track_id) DO UPDATE SET
                     target_track_id=excluded.target_track_id, reason=excluded.reason""",
                (mapping_id, source_side, source_track_id, target_track_id, reason, utcnow()),
            )

    def delete_override(self, mapping_id: str, source_side: str, source_track_id: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM overrides WHERE mapping_id=? AND source_side=? AND source_track_id=?",
                (mapping_id, source_side, source_track_id),
            )

    def overrides_for(self, mapping_id: str, source_side: str) -> dict[str, str]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT source_track_id, target_track_id FROM overrides WHERE mapping_id=? AND source_side=?",
                (mapping_id, source_side),
            )
            return {row["source_track_id"]: row["target_track_id"] for row in cur.fetchall()}

    # -- snapshots -------------------------------------------------------------

    def save_snapshot(
        self,
        *,
        mapping_id: str | None,
        side: str,
        playlist_id: str | None,
        kind: str,
        entries: list[dict[str, Any]],
        fingerprint: str,
        run_id: int | None = None,
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO snapshots (mapping_id, side, playlist_id, kind, entries,
                        track_ids, fingerprint, created_at, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    mapping_id,
                    side,
                    playlist_id,
                    kind,
                    json.dumps(entries),
                    json.dumps([e.get("track_id") for e in entries]),
                    fingerprint,
                    utcnow(),
                    run_id,
                ),
            )
            return int(cur.lastrowid)

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_snapshots(self, mapping_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            if mapping_id:
                cur.execute(
                    "SELECT * FROM snapshots WHERE mapping_id=? ORDER BY id DESC LIMIT ?",
                    (mapping_id, limit),
                )
            else:
                cur.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    # -- baselines -------------------------------------------------------------

    def set_baseline(
        self,
        mapping_id: str,
        *,
        plex_fingerprint: str,
        gonic_fingerprint: str,
        plex_entries: list[dict[str, Any]],
        gonic_entries: list[dict[str, Any]],
        canonical: list[dict[str, Any]],
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO baselines (mapping_id, plex_fingerprint, gonic_fingerprint,
                        plex_entries, gonic_entries, canonical, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(mapping_id) DO UPDATE SET
                     plex_fingerprint=excluded.plex_fingerprint,
                     gonic_fingerprint=excluded.gonic_fingerprint,
                     plex_entries=excluded.plex_entries,
                     gonic_entries=excluded.gonic_entries,
                     canonical=excluded.canonical,
                     updated_at=excluded.updated_at""",
                (
                    mapping_id,
                    plex_fingerprint,
                    gonic_fingerprint,
                    json.dumps(plex_entries),
                    json.dumps(gonic_entries),
                    json.dumps(canonical),
                    utcnow(),
                ),
            )

    def get_baseline(self, mapping_id: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM baselines WHERE mapping_id=?", (mapping_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    # -- runs ------------------------------------------------------------------

    def start_run(self, mapping_id: str | None, mode: str, trigger: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO runs (mapping_id, mode, trigger, status, started_at)
                   VALUES (?,?,?,?,?)""",
                (mapping_id, mode, trigger, "running", utcnow()),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        stats: dict[str, Any] | None = None,
        error: str = "",
        *,
        error_code: str | None = None,
        verified: bool | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE runs SET status=?, finished_at=?, stats=?, error=?, error_code=?, verified=? WHERE id=?",
                (
                    status,
                    utcnow(),
                    json.dumps(stats or {}),
                    error,
                    error_code,
                    None if verified is None else int(verified),
                    run_id,
                ),
            )

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    # -- journal (fine-grained, kept for diagnostics) --------------------------

    def journal_start(
        self, run_id: int | None, mapping_id: str, side: str, operation: str, payload: dict[str, Any]
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO journal (run_id, mapping_id, side, operation, payload, status, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, mapping_id, side, operation, json.dumps(payload), "started", utcnow()),
            )
            return int(cur.lastrowid)

    def journal_finish(
        self, entry_id: int, status: str, detail: str = "", payload: dict[str, Any] | None = None
    ) -> None:
        with self._cursor() as cur:
            if payload is not None:
                cur.execute(
                    "UPDATE journal SET status=?, completed_at=?, detail=?, payload=? WHERE id=?",
                    (status, utcnow(), detail, json.dumps(payload), entry_id),
                )
            else:
                cur.execute(
                    "UPDATE journal SET status=?, completed_at=?, detail=? WHERE id=?",
                    (status, utcnow(), detail, entry_id),
                )

    def journal_incomplete(self) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM journal WHERE status='started' ORDER BY id")
            return [dict(row) for row in cur.fetchall()]

    # -- operations (durable lifecycle) ----------------------------------------

    def create_operation(self, **fields: Any) -> str:
        key = fields.pop("operation_key", None) or str(uuid.uuid4())
        now = utcnow()
        values = {
            "operation_key": key,
            "mapping_id": fields.get("mapping_id"),
            "run_id": fields.get("run_id"),
            "direction": fields.get("direction"),
            "source_side": fields.get("source_side"),
            "target_side": fields.get("target_side"),
            "source_playlist_id": fields.get("source_playlist_id"),
            "target_playlist_id": fields.get("target_playlist_id"),
            "intended_name": fields.get("intended_name"),
            "state": fields.get("state", OperationState.PLANNED),
            "step": fields.get("step", "planned"),
            "source_fingerprint": fields.get("source_fingerprint"),
            "target_fingerprint_before": fields.get("target_fingerprint_before"),
            "desired_fingerprint": fields.get("desired_fingerprint"),
            "target_fingerprint_after": fields.get("target_fingerprint_after"),
            "desired_ids": json.dumps(fields.get("desired_ids") or []),
            "snapshot_id": fields.get("snapshot_id"),
            "created_playlist_id": fields.get("created_playlist_id"),
            "attempt": fields.get("attempt", 0),
            "error_code": fields.get("error_code"),
            "detail": fields.get("detail"),
            "created_at": now,
            "updated_at": now,
        }
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO operations (operation_key, mapping_id, run_id, direction,
                        source_side, target_side, source_playlist_id, target_playlist_id,
                        intended_name, state, step, source_fingerprint, target_fingerprint_before,
                        desired_fingerprint, target_fingerprint_after, desired_ids, snapshot_id,
                        created_playlist_id, attempt, error_code, detail, created_at, updated_at)
                   VALUES (:operation_key,:mapping_id,:run_id,:direction,:source_side,:target_side,
                        :source_playlist_id,:target_playlist_id,:intended_name,:state,:step,
                        :source_fingerprint,:target_fingerprint_before,:desired_fingerprint,
                        :target_fingerprint_after,:desired_ids,:snapshot_id,:created_playlist_id,
                        :attempt,:error_code,:detail,:created_at,:updated_at)""",
                values,
            )
        return key

    def operation_progress(self, operation_key: str, **fields: Any) -> None:
        """Record progress for an operation and bump its revision.

        Reporting failures here must never corrupt authoritative state, so this
        is separate from the state transitions in :meth:`update_operation`.
        """
        allowed = {
            "state", "step", "stage", "stage_desc", "completed", "total", "unit",
            "counters", "message", "heartbeat_at", "cancel_requested", "started_at",
            "error_code", "detail", "target_fingerprint_after",
            "created_playlist_id", "target_playlist_id",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "counters" and not isinstance(value, str):
                value = json.dumps(value)
            if key == "cancel_requested":
                value = int(bool(value))
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        sets.append("revision=revision+1")
        sets.append("updated_at=?")
        values.append(utcnow())
        values.append(operation_key)
        with self._cursor() as cur:
            cur.execute(f"UPDATE operations SET {', '.join(sets)} WHERE operation_key=?", values)

    def request_cancel(self, operation_key: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE operations SET cancel_requested=1, revision=revision+1, updated_at=? "
                "WHERE operation_key=?",
                (utcnow(), operation_key),
            )

    def operation_snapshot(self, operation_key: str) -> dict[str, Any] | None:
        operation = self.get_operation(operation_key)
        return _operation_snapshot(operation) if operation else None

    def active_operation_snapshots(self) -> list[dict[str, Any]]:
        return [_operation_snapshot(op) for op in self.list_active_operations()]

    def update_operation(self, operation_key: str, **fields: Any) -> None:
        allowed = {
            "state",
            "step",
            "target_fingerprint_after",
            "snapshot_id",
            "created_playlist_id",
            "attempt",
            "error_code",
            "detail",
            "target_playlist_id",
            "mapping_id",
        }
        sets = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "desired_ids" and not isinstance(value, str):
                value = json.dumps(value)
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        values.append(utcnow())
        values.append(operation_key)
        with self._cursor() as cur:
            cur.execute(f"UPDATE operations SET {', '.join(sets)} WHERE operation_key=?", values)

    def get_operation(self, operation_key: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM operations WHERE operation_key=?", (operation_key,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_operations(
        self,
        *,
        mapping_id: str | None = None,
        states: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if mapping_id:
            clauses.append("mapping_id=?")
            values.append(mapping_id)
        if states is not None:
            state_list = list(states)
            if not state_list:
                return []
            clauses.append(f"state IN ({','.join('?' for _ in state_list)})")
            values.extend(state_list)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM operations {where} ORDER BY id DESC LIMIT ?", (*values, limit)
            )
            return [dict(row) for row in cur.fetchall()]

    def list_active_operations(self) -> list[dict[str, Any]]:
        return self.list_operations(states=ACTIVE_OPERATION_STATES, limit=200)

    # -- conflicts -------------------------------------------------------------

    def record_conflict(
        self,
        mapping_id: str,
        run_id: int | None,
        plex_entries: list[dict[str, Any]],
        gonic_entries: list[dict[str, Any]],
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO conflicts (mapping_id, run_id, plex_entries, gonic_entries, created_at)
                   VALUES (?,?,?,?,?)""",
                (mapping_id, run_id, json.dumps(plex_entries), json.dumps(gonic_entries), utcnow()),
            )
            return int(cur.lastrowid)

    def list_conflicts(self, *, unresolved_only: bool = True) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            if unresolved_only:
                cur.execute("SELECT * FROM conflicts WHERE resolved=0 ORDER BY id DESC")
            else:
                cur.execute("SELECT * FROM conflicts ORDER BY id DESC")
            return [dict(row) for row in cur.fetchall()]

    def resolve_conflict(self, conflict_id: int, resolution: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE conflicts SET resolved=1, resolution=? WHERE id=?", (resolution, conflict_id)
            )

    # -- locks -----------------------------------------------------------------

    def acquire_lock(self, name: str, owner: str, ttl_seconds: int = 900, *, wait_seconds: float = 0.0) -> bool:
        """Acquire a named lock. Waits up to ``wait_seconds`` for contention.

        A lock is only reclaimed when it has expired (TTL). We never steal a
        lock merely because an operation is slow: callers renew via heartbeat.
        """
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            now = datetime.now(UTC)
            with self._cursor() as cur:
                cur.execute("DELETE FROM locks WHERE expires_at < ?", (now.isoformat(),))
                cur.execute(
                    """INSERT INTO locks (name, owner, acquired_at, heartbeat_at, expires_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(name) DO NOTHING""",
                    (
                        name,
                        owner,
                        now.isoformat(),
                        now.isoformat(),
                        (now + timedelta(seconds=ttl_seconds)).isoformat(),
                    ),
                )
                if cur.rowcount == 1:
                    return True
                cur.execute("SELECT owner FROM locks WHERE name=?", (name,))
                row = cur.fetchone()
                if row and row["owner"] == owner:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def heartbeat_lock(self, name: str, owner: str, ttl_seconds: int = 900) -> bool:
        now = datetime.now(UTC)
        with self._cursor() as cur:
            cur.execute(
                "UPDATE locks SET heartbeat_at=?, expires_at=? WHERE name=? AND owner=?",
                (now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat(), name, owner),
            )
            return cur.rowcount == 1

    def release_lock(self, name: str, owner: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    def lock_owner(self, name: str) -> str | None:
        with self._cursor() as cur:
            cur.execute("SELECT owner FROM locks WHERE name=?", (name,))
            row = cur.fetchone()
            return row["owner"] if row else None

    # -- retention -------------------------------------------------------------

    def prune_history(
        self,
        *,
        runs_keep: int = 500,
        snapshots_keep: int = 200,
    ) -> dict[str, int]:
        """Prune old history, never removing state needed by active operations."""
        removed = {"runs": 0, "snapshots": 0}
        with self._cursor() as cur:
            cur.execute(
                "SELECT snapshot_id FROM operations WHERE snapshot_id IS NOT NULL AND state IN "
                f"({','.join('?' for _ in ACTIVE_OPERATION_STATES)})",
                tuple(ACTIVE_OPERATION_STATES),
            )
            protected = {int(r["snapshot_id"]) for r in cur.fetchall() if r["snapshot_id"] is not None}

            cur.execute("SELECT COUNT(*) AS c FROM runs")
            total_runs = int(cur.fetchone()["c"])
            if total_runs > runs_keep:
                cur.execute(
                    "DELETE FROM runs WHERE id IN (SELECT id FROM runs ORDER BY id ASC LIMIT ?)",
                    (total_runs - runs_keep,),
                )
                removed["runs"] = cur.rowcount

            cur.execute("SELECT id FROM snapshots ORDER BY id DESC")
            all_ids = [int(r["id"]) for r in cur.fetchall()]
            keep = set(all_ids[:snapshots_keep]) | protected
            to_delete = [sid for sid in all_ids if sid not in keep]
            for start in range(0, len(to_delete), 500):
                chunk = to_delete[start : start + 500]
                cur.execute(
                    f"DELETE FROM snapshots WHERE id IN ({','.join('?' for _ in chunk)})",
                    tuple(chunk),
                )
                removed["snapshots"] += cur.rowcount
        return removed

    # -- storage health --------------------------------------------------------

    def storage_health(self, *, min_free_bytes: int = 0) -> dict[str, Any]:
        health: dict[str, Any] = {
            "path": str(self.path),
            "schema_version": self.schema_version,
            "installation_id": self.installation_id,
            "writable": os.access(self.path.parent, os.W_OK),
            "integrity_ok": self.state_error is None or self.state_error.code != "STORAGE_CORRUPT",
            "mutations_allowed": self.mutations_allowed,
            "state_error": self.state_error.to_dict(include_details=True) if self.state_error else None,
        }
        try:
            usage = shutil.disk_usage(self.path.parent)
            health["free_bytes"] = usage.free
            health["total_bytes"] = usage.total
            health["low_free_space"] = usage.free < min_free_bytes
        except OSError:
            health["free_bytes"] = None
        return health

    def diagnostics(self) -> dict:
        """Read-only SQLite/storage diagnostics (no secrets, no DB contents)."""
        parent = self.path.parent
        info: dict[str, Any] = {
            "path": str(self.path),
            "realpath": os.path.realpath(self.path),
            "exists": self.path.exists(),
            "schema_version": self.schema_version,
            "journal_mode": getattr(self, "journal_mode", "unknown"),
            "sqlite_version": sqlite3.sqlite_version,
            "process_uid_gid": f"{os.getuid()}:{os.getgid()}",
            "parent": str(parent),
            "parent_exists": parent.exists(),
            "writable": os.access(parent, os.W_OK),
            "mutations_allowed": self.mutations_allowed,
            "state_error": self.state_error.to_dict(include_details=True) if self.state_error else None,
        }
        try:
            stat = parent.stat()
            info["parent_mode"] = oct(stat.st_mode & 0o777)
            info["parent_uid_gid"] = f"{stat.st_uid}:{stat.st_gid}"
        except OSError as exc:
            info["parent_stat_error"] = type(exc).__name__
        try:
            self._conn.execute("SELECT 1").fetchone()
            info["connection_ok"] = True
        except sqlite3.Error as exc:
            info["connection_ok"] = False
            info["connection_error"] = str(exc)
        # bounded directory write test (creates and removes a temp file)
        test = parent / f".pgs-write-test-{os.getpid()}"
        try:
            fd = os.open(test, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, b"x")
            os.close(fd)
            test.unlink()
            info["write_test"] = "ok"
        except OSError as exc:
            info["write_test"] = f"failed: {type(exc).__name__}"
        # can we open an auxiliary journal/wal file next to the db?
        aux = self.path.with_name(self.path.name + ".aux-test")
        try:
            aux.write_bytes(b"")
            aux.unlink()
            info["aux_file_test"] = "ok"
        except OSError as exc:
            info["aux_file_test"] = f"failed: {type(exc).__name__}"
        return info

    # -- recurring jobs --------------------------------------------------------

    def create_job(self, **fields: Any) -> dict:
        import uuid as _uuid

        job_id = fields.pop("id", None) or str(_uuid.uuid4())
        now = utcnow()
        values = {
            "id": job_id,
            "name": fields.get("name") or "job",
            "type": fields["type"],
            "params": json.dumps(fields.get("params") or {}),
            "frequency": json.dumps(fields.get("frequency") or {"kind": "manual"}),
            "timezone": fields.get("timezone") or "UTC",
            "run_mode": fields.get("run_mode") or "every_run",
            "enabled": int(bool(fields.get("enabled", True))),
            "paused": int(bool(fields.get("paused", False))),
            "approved": int(bool(fields.get("approved", False))),
            "next_run": fields.get("next_run"),
            "config_revision": int(fields.get("config_revision", 1)),
            "created_at": now,
            "updated_at": now,
        }
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO jobs (id, name, type, params, frequency, timezone, run_mode,
                        enabled, paused, approved, next_run, config_revision, created_at, updated_at)
                   VALUES (:id,:name,:type,:params,:frequency,:timezone,:run_mode,:enabled,
                        :paused,:approved,:next_run,:config_revision,:created_at,:updated_at)""",
                values,
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
            row = cur.fetchone()
            return self._job_row(row) if row else None

    def list_jobs(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM jobs ORDER BY name")
            return [self._job_row(row) for row in cur.fetchall()]

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "name", "type", "params", "frequency", "timezone", "run_mode", "enabled",
            "paused", "approved", "last_attempt", "last_success", "next_run",
            "last_occurrence", "last_result", "config_revision",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"enabled", "paused", "approved"}:
                value = int(bool(value))
            elif key in {"params", "frequency"} and not isinstance(value, str):
                value = json.dumps(value)
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        values.append(utcnow())
        values.append(job_id)
        with self._cursor() as cur:
            cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", values)

    def delete_job(self, job_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def due_jobs(self, now_iso: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM jobs WHERE enabled=1 AND paused=0 AND next_run IS NOT NULL AND next_run<=? "
                "ORDER BY next_run",
                (now_iso,),
            )
            return [self._job_row(row) for row in cur.fetchall()]

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict:
        job = dict(row)
        for key in ("params", "frequency"):
            try:
                job[key] = json.loads(job.get(key) or "{}")
            except (TypeError, ValueError):
                job[key] = {}
        raw = job.get("last_result")
        if raw:
            try:
                job["last_result"] = json.loads(raw)
            except (TypeError, ValueError):
                job["last_result"] = {"status": str(raw)}
        else:
            job["last_result"] = {}
        for key in ("enabled", "paused", "approved"):
            job[key] = bool(job.get(key))
        return job
