from playlist_sync.config import (
    AppSettings,
    PathRootSettings,
    validate_config,
)


def fields(errors):
    return {e["field"] for e in errors}


def test_valid_defaults_pass():
    assert validate_config(AppSettings()) == []


def test_invalid_urls_are_reported():
    settings = AppSettings()
    settings.plex.url = "not a url"
    settings.gonic.url = "ftp://nope"
    errs = fields(validate_config(settings))
    assert "plex.url" in errs
    assert "gonic.url" in errs


def test_overlapping_path_roots_are_reported():
    settings = AppSettings()
    settings.sync.path_roots = [
        PathRootSettings(name="a", plex_root="/music", gonic_root="/m"),
        PathRootSettings(name="b", plex_root="/music/rock", gonic_root="/m/rock"),
    ]
    errs = fields(validate_config(settings))
    assert any("path_roots" in f for f in errs)


def test_same_plex_root_to_two_gonic_roots_is_reported():
    settings = AppSettings()
    settings.sync.path_roots = [
        PathRootSettings(name="a", plex_root="/music", gonic_root="/m1"),
        PathRootSettings(name="b", plex_root="/music", gonic_root="/m2"),
    ]
    errs = validate_config(settings)
    assert any("multiple gonic roots" in e["message"] for e in errs)


def test_bad_intervals_and_tolerances_are_reported():
    settings = AppSettings()
    settings.sync.poll_seconds = -5
    settings.sync.duration_tolerance_ms = 10_000_000
    errs = fields(validate_config(settings))
    assert "sync.poll_seconds" in errs
    assert "sync.duration_tolerance_ms" in errs


def test_secret_file_errors_are_surfaced():
    errs = validate_config(
        AppSettings(), secret_file_errors=[{"field": "plex.token", "message": "not readable"}]
    )
    assert any(e["field"] == "plex.token" for e in errs)


def test_credentials_required_when_requested():
    settings = AppSettings()
    errs = fields(validate_config(settings, require_credentials=True))
    assert {"plex.token", "gonic.username", "gonic.password"} <= errs


def test_load_settings_raises_on_invalid_configuration(monkeypatch):
    import pytest

    from playlist_sync.config import load_settings
    from playlist_sync.errors import AppError

    monkeypatch.setenv("PLAYLIST_SYNC_SYNC__POLL_SECONDS", "-1")
    with pytest.raises(AppError) as excinfo:
        load_settings()
    assert excinfo.value.code == "CONFIG_INVALID"
    assert excinfo.value.details["fields"]
