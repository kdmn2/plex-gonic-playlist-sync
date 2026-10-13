import os
import sqlite3

import pytest

from playlist_sync.errors import AppError
from playlist_sync.store import SCHEMA_VERSION, OperationState, Store


def test_fresh_install_writes_identity(tmp_path):
    store = Store(tmp_path / "data" / "state.sqlite3", tmp_path / "config" / "installation.json")
    try:
        assert store.installation_id
        assert store.meta_get("installation_id") == store.installation_id
        assert (tmp_path / "config" / "installation.json").exists()
        assert store.schema_version == SCHEMA_VERSION
        assert store.mutations_allowed
    finally:
        store.close()


def test_missing_data_mount_is_detected(tmp_path):
    config = tmp_path / "config" / "installation.json"
    first = Store(tmp_path / "data1" / "state.sqlite3", config)
    first.close()
    # Same installation (marker exists) but a brand-new, empty data mount.
    second = Store(tmp_path / "data2" / "state.sqlite3", config)
    try:
        assert second.state_error is not None
        assert second.state_error.code == "STATE_MISSING_MOUNT"
        assert second.mutations_allowed is False
    finally:
        second.close()


def test_identity_mismatch_is_detected(tmp_path):
    db = tmp_path / "data" / "state.sqlite3"
    store = Store(db)
    assert store.installation_id
    store.close()
    marker = tmp_path / "config" / "installation.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"installation_id": "a-different-id"}')
    reopened = Store(db, marker)
    try:
        assert reopened.state_error is not None
        assert reopened.state_error.code == "STATE_IDENTITY_MISMATCH"
        assert reopened.mutations_allowed is False
    finally:
        reopened.close()


def test_newer_schema_fails_closed(tmp_path):
    db = tmp_path / "state.sqlite3"
    store = Store(db)
    store._conn.execute("PRAGMA user_version=999")
    store._conn.commit()
    store.close()
    reopened = Store(db)
    try:
        assert reopened.state_error is not None
        assert reopened.state_error.code == "STATE_SCHEMA_UNSUPPORTED"
        assert reopened.mutations_allowed is False
    finally:
        reopened.close()


def test_corrupt_database_is_recorded_not_crashed(tmp_path):
    db = tmp_path / "state.sqlite3"
    db.write_bytes(b"this is not a sqlite database at all")
    store = Store(db)
    try:
        assert store.state_error is not None
        assert store.state_error.code in {"STORAGE_CORRUPT", "STORAGE_UNAVAILABLE"}
        assert store.mutations_allowed is False
        # diagnostics remain available without raising
        health = store.storage_health()
        assert health["mutations_allowed"] is False
    finally:
        store.close()


def test_legacy_schema_is_backed_up_and_migrated(tmp_path):
    db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE mappings (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("PRAGMA user_version=0")
    conn.commit()
    conn.close()

    store = Store(db)
    try:
        assert store.schema_version == SCHEMA_VERSION
        backups = list(tmp_path.glob("state.sqlite3.bak-*"))
        assert backups, "a pre-migration backup should exist"
    finally:
        store.close()


def test_read_only_state_directory_is_refused(tmp_path):
    directory = tmp_path / "ro"
    directory.mkdir()
    os.chmod(directory, 0o555)
    try:
        with pytest.raises(AppError) as excinfo:
            Store(directory / "state.sqlite3")
        assert excinfo.value.code == "STORAGE_READ_ONLY"
    finally:
        os.chmod(directory, 0o755)


def test_prune_never_removes_snapshots_needed_by_active_operations(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        protected = store.save_snapshot(
            mapping_id="m", side="gonic", playlist_id="pl", kind="pre_write",
            entries=[{"track_id": "t1"}], fingerprint="fp",
        )
        store.create_operation(
            mapping_id="m", target_side="gonic", state=OperationState.APPLYING,
            snapshot_id=protected, desired_ids=["t1"],
        )
        for i in range(10):
            store.save_snapshot(
                mapping_id="m", side="gonic", playlist_id="pl", kind="last_success",
                entries=[{"track_id": f"t{i}"}], fingerprint=f"fp{i}",
            )
        store.prune_history(snapshots_keep=1)
        assert store.get_snapshot(protected) is not None
    finally:
        store.close()


def test_operation_lifecycle_persists(tmp_path):
    from playlist_sync.store import ACTIVE_OPERATION_STATES

    store = Store(tmp_path / "state.sqlite3")
    try:
        key = store.create_operation(
            mapping_id="m", target_side="gonic", state=OperationState.PLANNED,
            desired_ids=["a", "b"], desired_fingerprint="fp",
        )
        op = store.get_operation(key)
        assert op["state"] == OperationState.PLANNED
        assert OperationState.PLANNED in ACTIVE_OPERATION_STATES
        store.update_operation(key, state=OperationState.SUCCEEDED, step="verified")
        assert store.get_operation(key)["state"] == OperationState.SUCCEEDED
        assert store.list_active_operations() == []
    finally:
        store.close()
