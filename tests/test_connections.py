import pytest

from playlist_sync import connections, secretstore, settings_store
from playlist_sync.config import GonicSettings, PlexSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "PLAYLIST_SYNC_PLEX__URL",
        "PLAYLIST_SYNC_PLEX__TOKEN",
        "PLAYLIST_SYNC_PLEX__TOKEN_FILE",
        "PLAYLIST_SYNC_PLEX__LIBRARY_SECTIONS",
        "PLAYLIST_SYNC_GONIC__URL",
        "PLAYLIST_SYNC_GONIC__USERNAME",
        "PLAYLIST_SYNC_GONIC__PASSWORD",
        "PLAYLIST_SYNC_GONIC__MUSIC_FOLDER_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def fake_test(report=None):
    report = report or {
        "plex": {"reachable": True, "authenticated": True, "identity": "m-1", "url": "http://plex-new:32400"},
        "gonic": {"reachable": True, "authenticated": True, "identity": "http://gonic-new:4747", "url": "http://gonic-new:4747"},
    }

    def _t(_draft):
        return report

    return _t


def base_settings(tmp_path, **plex_kw):
    return __import__("playlist_sync.config", fromlist=["AppSettings"]).AppSettings(
        config_dir=tmp_path,
        data_dir=tmp_path,
        plex=PlexSettings(url="http://plex-old:32400", token="old-token-123456", **plex_kw),
        gonic=GonicSettings(url="http://gonic-old:4747", username="olduser", password="oldpass"),
    )


def test_state_reports_configured_without_exposing_secret(tmp_path):
    settings = base_settings(tmp_path)
    state = connections.connection_state(settings)
    assert state["plex"]["configured"] is True
    assert state["gonic"]["configured"] is True
    assert "old-token-123456" not in str(state)
    assert "oldpass" not in str(state)


def test_external_fields_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYLIST_SYNC_GONIC__URL", "http://external-gonic:4747")
    settings = base_settings(tmp_path)
    state = connections.connection_state(settings)
    assert state["fields"]["gonic.url"]["external"] is True
    # a draft cannot override an external field
    draft = connections.build_draft(settings, {"gonic_url": "http://ignored:1"})
    assert draft.gonic.url != "http://ignored:1"


def test_untouched_credential_is_preserved(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    monkeypatch.setattr(connections, "test_connection", fake_test())
    secretstore.write_managed_secret(tmp_path, "gonic_password", "managed-pw-123")
    result = connections.save_connection(settings, {"gonic_url": "http://gonic-new:4747"})
    assert result.ok
    assert secretstore.read_managed_secret(tmp_path, "gonic_password") == "managed-pw-123"


def test_masked_placeholder_is_never_saved(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    settings.gonic.password = ""  # no external password
    monkeypatch.setattr(connections, "test_connection", fake_test())
    connections.save_connection(
        settings, {"replace_gonic_password": "on", "gonic_password": "********"}
    )
    assert secretstore.read_managed_secret(tmp_path, "gonic_password") is None


def test_replace_password_is_saved(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    settings.gonic.password = ""
    monkeypatch.setattr(connections, "test_connection", fake_test())
    result = connections.save_connection(
        settings, {"replace_gonic_password": "on", "gonic_password": "brand-new-pw"}
    )
    assert result.ok
    assert secretstore.read_managed_secret(tmp_path, "gonic_password") == "brand-new-pw"


def test_save_persists_nonsecret_and_survives_restart(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    monkeypatch.setattr(connections, "test_connection", fake_test())
    result = connections.save_connection(
        settings,
        {
            "gonic_url": "http://gonic-new:4747",
            "gonic_username": "newuser",
            "plex_url": "http://plex-new:32400",
        },
    )
    assert result.ok
    assert settings.gonic.url == "http://gonic-new:4747"
    # a fresh settings object overlays the managed values (restart survival)
    from playlist_sync.config import AppSettings

    reloaded = AppSettings(config_dir=tmp_path, data_dir=tmp_path)
    reloaded.apply_managed()
    assert reloaded.gonic.url == "http://gonic-new:4747"
    assert reloaded.gonic.username == "newuser"
    assert reloaded.plex.url == "http://plex-new:32400"


def test_atomic_save_failure_keeps_previous(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    monkeypatch.setattr(connections, "test_connection", fake_test())
    assert connections.save_connection(settings, {"gonic_url": "http://gonic-new:4747"}).ok
    previous = settings_store.load_managed(tmp_path)
    monkeypatch.setattr(settings_store, "save_managed", lambda *a, **k: False)
    result = connections.save_connection(settings, {"gonic_url": "http://gonic-other:4747"})
    assert result.ok is False
    assert settings_store.load_managed(tmp_path) == previous


def test_server_change_requires_confirmation(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    settings_store.save_managed(
        tmp_path,
        {"validated": {"gonic": {"url": "http://gonic-old:4747", "identity": "x", "when": "t"}}},
    )
    monkeypatch.setattr(connections, "test_connection", fake_test())
    result = connections.save_connection(settings, {"gonic_url": "http://gonic-new:4747"})
    assert result.ok is False
    assert result.needs_confirmation is True
    assert result.confirmation_details
    confirmed = connections.save_connection(
        settings, {"gonic_url": "http://gonic-new:4747", "confirm_change": "on"}, confirm_change=True
    )
    assert confirmed.ok
    assert confirmed.server_changed is True
    assert "gonic" in confirmed.changed_sides


def test_invalid_url_is_rejected(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    monkeypatch.setattr(connections, "test_connection", fake_test())
    result = connections.save_connection(settings, {"gonic_url": "not-a-url"})
    assert result.ok is False
    assert any(e["field"] == "gonic.url" for e in result.errors)


def test_remove_secret_only_managed(tmp_path):
    settings = base_settings(tmp_path)
    secretstore.write_managed_secret(tmp_path, "gonic_password", "managed-pw")
    assert connections.remove_secret(settings, "gonic.password") is True
    assert secretstore.read_managed_secret(tmp_path, "gonic_password") is None
    # external fields cannot be removed
    assert connections.remove_secret(settings, "nope") is False


def test_test_connection_real_invalid_host_is_bounded(tmp_path):
    settings = base_settings(tmp_path)
    settings.plex.url = "http://127.0.0.1:1"
    settings.gonic.url = "http://127.0.0.1:1"
    settings.plex.token = "x"
    settings.gonic.password = "y"
    report = connections.test_connection(settings)
    assert report["plex"]["authenticated"] is False
    assert report["gonic"]["authenticated"] is False
