
from playlist_sync.config import AppSettings, load_settings, materialize_secret_files


def test_env_override_nested(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYLIST_SYNC_PLEX__URL", "http://from-env:32400")
    monkeypatch.setenv("PLAYLIST_SYNC_GONIC__TIMEOUT", "9")
    settings = AppSettings()
    assert settings.plex.url == "http://from-env:32400"
    assert settings.gonic.timeout == 9


def test_secret_file_materialization(monkeypatch, tmp_path):
    secret_file = tmp_path / "plex_token"
    secret_file.write_text("file-token-value\n")
    env = {"PLAYLIST_SYNC_PLEX__TOKEN_FILE": str(secret_file)}
    materialize_secret_files(env)
    assert env["PLAYLIST_SYNC_PLEX__TOKEN"] == "file-token-value"


def test_yaml_layering(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
plex:
  url: http://yaml-plex:32400
sync:
  poll_seconds: 42
  path_roots:
    - name: r
      plex_root: /a
      gonic_root: /b
"""
    )
    monkeypatch.setenv("PLAYLIST_SYNC_CONFIG_FILE", str(config))
    settings = load_settings()
    assert settings.plex.url == "http://yaml-plex:32400"
    assert settings.sync.poll_seconds == 42
    assert settings.sync.path_roots[0].plex_root == "/a"


def test_env_beats_yaml(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("plex:\n  url: http://yaml-plex:32400\n")
    monkeypatch.setenv("PLAYLIST_SYNC_CONFIG_FILE", str(config))
    monkeypatch.setenv("PLAYLIST_SYNC_PLEX__URL", "http://env-plex:32400")
    settings = load_settings()
    assert settings.plex.url == "http://env-plex:32400"


def test_verify_for_ca_bundle(tmp_path):
    settings = AppSettings()
    settings.plex.verify_tls = True
    assert settings.verify_for("plex") is True
    settings.plex.ca_bundle = "/etc/ssl/custom.pem"
    assert settings.verify_for("plex") == "/etc/ssl/custom.pem"
    settings.gonic.verify_tls = False
    assert settings.verify_for("gonic") is False


def test_path_mapper_from_settings(tmp_path):
    from playlist_sync.config import build_path_mapper

    settings = AppSettings.model_validate(
        {"sync": {"path_roots": [{"name": "r", "plex_root": "/p", "gonic_root": "/g"}]}}
    )
    mapper = build_path_mapper(settings)
    assert mapper.configured
    assert mapper.strip_plex("/p/x.flac") == ["x.flac"]
