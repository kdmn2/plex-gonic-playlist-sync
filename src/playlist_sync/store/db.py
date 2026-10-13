"""SQLite-backed state: mappings, snapshots, baselines, runs, journal, locks.

No external database is used. Access is serialised through a process lock plus
SQLite's own locking; ``check_same_thread=False`` lets the web layer run the
synchronous engine from a worker thread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
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
    error TEXT
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
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


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
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

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

    # -- playlist cache --------------------------------------------------------

    def replace_playlist_cache(
        self, kind: str, identity: str, playlists: Iterable[Any]
    ) -> None:
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
                (mapping_id, mode, trigger, "started", utcnow()),
            )
            return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, status: str, stats: dict[str, Any] | None = None, error: str = ""
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE runs SET status=?, finished_at=?, stats=?, error=? WHERE id=?",
                (status, utcnow(), json.dumps(stats or {}), error, run_id),
            )

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    # -- journal ---------------------------------------------------------------

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

    def acquire_lock(self, name: str, owner: str, ttl_seconds: int = 900) -> bool:
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
            return bool(row and row["owner"] == owner)

    def release_lock(self, name: str, owner: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    def lock_owner(self, name: str) -> str | None:
        with self._cursor() as cur:
            cur.execute("SELECT owner FROM locks WHERE name=?", (name,))
            row = cur.fetchone()
            return row["owner"] if row else None
