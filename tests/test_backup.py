import hashlib
import json
from pathlib import Path

from playlist_sync import backup
from playlist_sync.models import ServerKind


def setup_backups(world):
    world.settings.backups.directory = str(world.tmp_path / "backups")
    world.settings.backups.path_mode = "server"
    world.settings.backups.require_all_entries = True
    return world.tmp_path / "backups"


def test_sanitize_name_and_label():
    assert "/" not in backup.sanitize_name("AC/DC: Best")
    assert ".." not in backup.sanitize_name("../../etc/passwd")
    assert "\n" not in backup.sanitize_label("a\nb\r c")
    assert backup.sanitize_name("Café ☕ 世界")  # unicode preserved (safe chars)


def test_validate_directory_ok_and_refuses_config_and_secrets(world, tmp_path):
    world.settings.backups.directory = str(tmp_path / "ok-backups")
    assert backup.validate_directory(world.settings)["ok"] is True
    world.settings.backups.directory = str(world.settings.config_dir)
    assert backup.validate_directory(world.settings)["ok"] is False
    world.settings.backups.directory = str(world.settings.config_dir / "secrets")
    assert backup.validate_directory(world.settings)["ok"] is False


def test_backup_gonic_m3u_order_duplicates_and_manifest(world):
    setup_backups(world)
    _, a = world.add_track("A/a.flac")
    _, b = world.add_track("A/b.flac")
    gid = world.gonic_playlist("Roadtrip", [a, b, a])
    result = backup.backup_playlist(world.ctx, ServerKind.GONIC, gid)
    assert result.status == "ok"
    text = Path(result.file).read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U")
    assert text.count("#EXTINF:") == 3
    # order + duplicates preserved (a, b, a); path lines are the non-comment lines
    paths = [line for line in text.splitlines() if not line.startswith("#")]
    assert paths == ["A/a.flac", "A/b.flac", "A/a.flac"]
    data = Path(result.file).read_bytes()
    assert hashlib.sha256(data).hexdigest() == result.checksum
    manifest = json.loads(Path(result.manifest).read_text(encoding="utf-8"))
    assert manifest["checksum_sha256"] == result.checksum
    assert manifest["entry_count"] == 3
    assert "creds" not in json.dumps(manifest).lower()


def test_versioned_backups_do_not_overwrite(world):
    setup_backups(world)
    _, a = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Mix", [a])
    first = backup.backup_playlist(world.ctx, ServerKind.GONIC, gid)
    first_bytes = Path(first.file).read_bytes()
    second = backup.backup_playlist(world.ctx, ServerKind.GONIC, gid)
    assert second.file != first.file
    assert Path(first.file).read_bytes() == first_bytes  # previous untouched


def test_mapped_and_relative_path_modes(world):
    setup_backups(world)
    world.settings.backups.path_mode = "mapped"
    from playlist_sync.config import BackupMappingSettings

    world.settings.backups.mappings = [
        BackupMappingSettings(server="gonic", from_root="A", to_root="/export/music")
    ]
    _, a = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Mix", [a])
    assert backup.export_path(
        world.gonic_client.get_playlist(gid).entries[0].track, ServerKind.GONIC, world.settings
    ) == "/export/music/a.flac"


def test_missing_path_blocks_by_default_and_partial_records_skips(world):
    setup_backups(world)
    _, a = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Mix", [a])
    # remove the path from the song -> unexportable
    song = next(iter(world.gonic.songs.values()))
    song.path = ""
    blocked = backup.backup_playlist(world.ctx, ServerKind.GONIC, gid)
    assert blocked.status == "blocked"
    assert not blocked.file
    partial = backup.backup_playlist(world.ctx, ServerKind.GONIC, gid, allow_partial=True)
    assert partial.status in {"partial", "empty"}
    assert partial.manifest
    manifest = json.loads(Path(partial.manifest).read_text(encoding="utf-8"))
    assert manifest["partial"] is True
    assert manifest["skipped"]


def test_empty_playlist_exports_valid_empty(world):
    setup_backups(world)
    gid = world.gonic_playlist("Empty", [])
    result = backup.backup_playlist(world.ctx, ServerKind.GONIC, gid)
    assert result.status == "empty"
    assert Path(result.file).read_text(encoding="utf-8").startswith("#EXTM3U")


def test_batch_isolation_and_summary(world):
    setup_backups(world)
    _, a = world.add_track("A/a.flac")
    good = world.gonic_playlist("Good", [a])
    results = backup.backup_many(
        world.ctx, [(ServerKind.GONIC, good), (ServerKind.GONIC, "pl-missing")]
    )
    assert results[0].status == "ok"
    assert results[1].status == "failed"
    summary = backup.summarize(results)
    assert summary["all_ok"] is False
    assert summary["counts"]["ok"] == 1


def test_last_backup_index(world):
    setup_backups(world)
    _, a = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Mix", [a])
    backup.backup_playlist(world.ctx, ServerKind.GONIC, gid)
    assert backup.last_backup(world.settings, ServerKind.GONIC, gid)["entries"] == 1


def test_preview_paths_reports_missing(world):
    setup_backups(world)
    _, a = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Mix", [a])
    preview = backup.preview_paths(world.ctx, ServerKind.GONIC, gid)
    assert preview["ok"] and preview["total"] == 1 and preview["missing"] == 0
