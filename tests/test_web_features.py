import re

import pytest
from fastapi.testclient import TestClient

from playlist_sync import connections
from playlist_sync.web.app import create_app


def fake_test(*_args, **_kwargs):
    return {
        "plex": {"reachable": True, "authenticated": True, "identity": "m-1", "url": "http://plex.test"},
        "gonic": {"reachable": True, "authenticated": True, "identity": "http://gonic.test", "url": "http://gonic.test"},
    }


@pytest.fixture
def client(world, monkeypatch):
    world.settings.ui.password = "ui-pass"
    world.settings.ui.username = "ui-user"
    world.settings.ui.session_secret = "s"
    world.settings.sync.poll_seconds = 0
    world.settings.plex.token = ""
    world.settings.gonic.password = ""
    monkeypatch.setattr(connections, "test_connection", fake_test)
    app = create_app(world.settings, ctx=world.ctx)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client):
    assert client.post("/login", data={"username": "ui-user", "password": "ui-pass"}).status_code == 303


def csrf(client, path):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"no csrf on {path}"
    return match.group(1)


def test_settings_page_requires_login(client):
    assert client.get("/settings/connections").status_code == 303


def test_settings_page_hides_secrets(client, world):
    world.settings.plex.token = "super-secret-token"
    world.settings.gonic.password = "super-secret-pass"
    login(client)
    text = client.get("/settings/connections").text
    assert "super-secret-token" not in text
    assert "super-secret-pass" not in text
    assert "Credential configured" in text


def test_settings_save_requires_csrf(client):
    login(client)
    assert client.post("/settings/connections/save", data={"gonic_url": "http://x"}).status_code == 403


def test_settings_save_redirects_and_persists(client, world):
    login(client)
    token = csrf(client, "/settings/connections")
    response = client.post(
        "/settings/connections/save",
        data={"csrf": token, "gonic_url": "http://gonic-new:4747", "gonic_username": "newuser"},
    )
    assert response.status_code == 303
    assert world.settings.gonic.url == "http://gonic-new:4747"
    from playlist_sync.settings_store import load_managed

    assert load_managed(world.settings.config_dir)["gonic"]["url"] == "http://gonic-new:4747"


def test_playlist_create_via_web(client, world):
    login(client)
    token = csrf(client, "/playlists/create")
    response = client.post(
        "/playlists/create",
        data={"csrf": token, "side": "gonic", "name": "Web Fresh", "submission_key": "k1"},
    )
    assert response.status_code == 200
    assert "created" in response.text
    assert any(p.name == "Web Fresh" for p in world.gonic.playlists.values())


def test_playlist_create_duplicate_warns_then_allows(client, world):
    _, ga = world.add_track("A/a.flac")
    existing = world.gonic_playlist("Mix", [ga])
    login(client)
    token = csrf(client, "/playlists/create")
    first = client.post(
        "/playlists/create", data={"csrf": token, "side": "gonic", "name": "Mix", "submission_key": "d1"}
    )
    assert "needs_confirmation" in first.text
    assert world.gonic.entries(existing) == [ga]  # untouched
    second = client.post(
        "/playlists/create",
        data={"csrf": token, "side": "gonic", "name": "Mix", "submission_key": "d2", "allow_duplicate": "on"},
    )
    assert "created" in second.text


def test_playlist_create_requires_csrf(client):
    login(client)
    assert client.post("/playlists/create", data={"side": "gonic", "name": "x"}).status_code == 403


def test_migrate_preview_and_apply_via_web(client, world):
    a, _ga = world.add_track("A/a.flac")
    b, _gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Roadtrip", [a, b, a])
    login(client)
    token = csrf(client, "/migrate")
    preview = client.post(
        "/migrate/preview",
        data={
            "csrf": token,
            "source_side": "plex",
            "source_playlist_id": str(pid),
            "target_side": "gonic",
            "name": "Roadtrip copy",
        },
    )
    assert preview.status_code == 200
    assert "Matched" in preview.text
    assert "original playlist will remain unchanged" in preview.text.lower()

    applied = client.post(
        "/migrate/apply",
        data={
            "csrf": token,
            "source_side": "plex",
            "source_playlist_id": str(pid),
            "target_side": "gonic",
            "name": "Roadtrip copy",
        },
    )
    assert applied.status_code == 200
    assert "copied" in applied.text.lower()
    assert any(p.name == "Roadtrip copy" for p in world.gonic.playlists.values())
    # source unchanged
    assert world.plex.get_playlist(str(pid)).track_ids() == [str(a), str(b), str(a)]


def test_migrate_requires_csrf(client):
    login(client)
    assert client.post("/migrate/preview", data={}).status_code == 403


def test_nav_consolidated_to_three_tabs(client):
    login(client)
    text = client.get("/").text
    assert 'href="/playlists"' in text
    assert 'href="/activity"' in text
    assert 'href="/settings"' in text


def test_new_hub_pages_render(client, world):
    login(client)
    assert client.get("/activity").status_code == 200
    assert client.get("/settings").status_code == 200
    backups_page = client.get("/settings/backups")
    assert backups_page.status_code == 200
    assert "backup" in backups_page.text.lower()


def test_backup_settings_save(client, world):
    login(client)
    token = csrf(client, "/settings/backups")
    response = client.post(
        "/settings/backups/save",
        data={"csrf": token, "backup_directory": str(world.tmp_path / "bk"), "backup_path_mode": "server"},
    )
    assert response.status_code == 303
    assert world.settings.backups.directory == str(world.tmp_path / "bk")


def test_backups_run_via_web_writes_file(client, world):
    world.settings.backups.directory = str(world.tmp_path / "bkruns")
    _, a = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Backup Me", [a])
    login(client)
    token = csrf(client, "/backups")
    response = client.post("/backups/run", data={"csrf": token, "selected": f"gonic:{gid}"})
    assert response.status_code == 200
    files = list((world.tmp_path / "bkruns").glob("*.m3u8"))
    assert files


def test_merge_page_and_preview_via_web(client, world):
    a, _ = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    pa = world.plex_playlist("A", [a])
    gb_pl = world.gonic_playlist("B", [gb])
    login(client)
    token = csrf(client, "/merge")
    preview = client.post(
        "/merge/preview",
        data={
            "csrf": token,
            "source_a_side": "plex",
            "source_a_id": str(pa),
            "source_b_side": "gonic",
            "source_b_id": gb_pl,
            "target_side": "gonic",
            "name": "Merged",
            "order": "ab",
            "policy": "keep_all",
        },
    )
    assert preview.status_code == 200
    assert "Preview" in preview.text
    applied = client.post(
        "/merge/apply",
        data={
            "csrf": token,
            "source_a_side": "plex",
            "source_a_id": str(pa),
            "source_b_side": "gonic",
            "source_b_id": gb_pl,
            "target_side": "gonic",
            "name": "Merged",
            "order": "ab",
            "policy": "keep_all",
        },
    )
    assert applied.status_code == 200
    assert "merged" in applied.text.lower()
    # sources unchanged
    assert world.plex.get_playlist(str(pa)).track_ids() == [str(a)]
    assert world.gonic.entries(gb_pl) == [gb]


def test_settings_save_path_roots(client, world):
    login(client)
    token = csrf(client, "/settings/connections")
    response = client.post(
        "/settings/connections/save",
        data={"csrf": token, "path_roots": "/plex-music => /gonic-music\n/data/music => /music"},
    )
    assert response.status_code == 303
    assert [(r.plex_root, r.gonic_root) for r in world.settings.sync.path_roots] == [
        ("/plex-music", "/gonic-music"),
        ("/data/music", "/music"),
    ]


def test_playlist_preview_page(client, world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Roadtrip", [a])
    login(client)
    # Default: entries render immediately; the (potentially slow) other-server
    # library index is NOT built, so the page cannot hang.
    response = client.get(f"/playlists/plex/{pid}")
    assert response.status_code == 200
    assert "Roadtrip" in response.text
    assert "A/a.flac" in response.text
    assert "Match status not computed" in response.text

    world.ctx.index_cache.clear()
    matched = client.get(f"/playlists/plex/{pid}?match=1")
    assert matched.status_code == 200
    assert "matched" in matched.text


def test_gonic_preview_does_not_build_plex_index_by_default(client, world):
    _, ga = world.add_track("A/a.flac")
    gid = world.gonic_playlist("G", [ga])
    world.ctx.index_cache.clear()
    login(client)
    response = client.get(f"/playlists/gonic/{gid}")
    assert response.status_code == 200
    assert "A/a.flac" in response.text


def test_playlists_page_links_to_preview(client, world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Roadtrip", [a])
    login(client)
    text = client.get("/playlists?server=plex").text
    # the playlist name and an explicit Preview button both link to the preview
    assert f'href="/playlists/plex/{pid}"' in text
    assert "Preview" in text
    assert f"source_playlist_id={pid}" in text  # Copy-to-new prefill


def test_playlist_preview_missing_playlist_is_a_page_not_a_crash(client):
    login(client)
    response = client.get("/playlists/plex/999999")
    assert response.status_code == 200
    assert "PLAYLIST_NOT_FOUND" in response.text


def test_migrate_preview_with_missing_source_is_not_a_crash(client):
    # Regression: a source playlist id that cannot be found must render a blocked
    # preview, not raise a template error (previously a 500).
    login(client)
    token = csrf(client, "/migrate")
    response = client.post(
        "/migrate/preview",
        data={
            "csrf": token,
            "source_side": "plex",
            "source_playlist_id": "999999",
            "target_side": "gonic",
            "name": "Nope",
        },
    )
    assert response.status_code == 200
    assert "no longer exists" in response.text.lower()
    assert "source playlist unavailable" in response.text.lower()


def test_migrate_preview_empty_source_is_not_a_crash(client):
    login(client)
    token = csrf(client, "/migrate")
    response = client.post(
        "/migrate/preview",
        data={"csrf": token, "source_side": "plex", "source_playlist_id": "", "target_side": "gonic", "name": ""},
    )
    assert response.status_code == 200
