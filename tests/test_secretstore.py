import os

from playlist_sync import secretstore
from playlist_sync.config import AppSettings, PlexSettings


def test_managed_token_roundtrip_permissions(tmp_path):
    assert secretstore.write_managed_token(tmp_path, "tok-1234567890abcd")
    path = secretstore.managed_token_path(tmp_path)
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert secretstore.read_managed_token(tmp_path) == "tok-1234567890abcd"
    assert secretstore.has_managed_token(tmp_path)
    assert secretstore.delete_managed_token(tmp_path)
    assert not secretstore.has_managed_token(tmp_path)


def test_failed_write_keeps_previous_credential(tmp_path, monkeypatch):
    assert secretstore.write_managed_token(tmp_path, "original-token-123456")
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "replace", boom)
    assert secretstore.write_managed_token(tmp_path, "new-token-123456") is False
    monkeypatch.setattr(os, "replace", real_replace)
    assert secretstore.read_managed_token(tmp_path) == "original-token-123456"


def test_precedence_external_token_wins(tmp_path):
    secretstore.write_managed_token(tmp_path, "managed-token-123456")
    settings = AppSettings(config_dir=tmp_path, plex=PlexSettings(url="http://p", token="external-token-123"))
    assert settings.plex_secret().get() == "external-token-123"
    assert settings.plex_credential_source() == "environment/config"
    assert settings.plex_external_configured is True


def test_precedence_external_file_over_managed(tmp_path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("file-token-123456\n")
    secretstore.write_managed_token(tmp_path, "managed-token-123456")
    settings = AppSettings(
        config_dir=tmp_path, plex=PlexSettings(url="http://p", token_file=str(token_file))
    )
    assert settings.plex_secret().get() == "file-token-123456"
    assert settings.plex_credential_source() == "token_file"


def test_managed_token_used_when_no_external(tmp_path):
    secretstore.write_managed_token(tmp_path, "managed-token-123456")
    settings = AppSettings(config_dir=tmp_path, plex=PlexSettings(url="http://p"), gonic={})
    assert settings.plex_secret().get() == "managed-token-123456"
    assert settings.plex_credential_source() == "ui-managed"
    assert settings.plex_external_configured is False
