import json
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
    assert applied.status_code == 303
    key = applied.headers["location"].rsplit("/", 1)[-1]
    import time

    snapshot = {}
    for _ in range(1000):
        snapshot = client.get(f"/api/operations/{key}").json()
        if snapshot.get("finished"):
            break
        time.sleep(0.02)
    assert snapshot.get("state") == "SUCCEEDED", snapshot
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
    assert response.status_code == 303
    key = response.headers["location"].rsplit("/", 1)[-1]
    import time

    snapshot = {}
    for _ in range(1000):
        snapshot = client.get(f"/api/operations/{key}").json()
        if snapshot.get("finished"):
            break
        time.sleep(0.02)
    assert snapshot.get("state") == "SUCCEEDED", snapshot
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
    # now runs in the background and returns an operation id promptly
    assert applied.status_code == 303
    key = applied.headers["location"].rsplit("/", 1)[-1]
    import time

    snapshot = {}
    for _ in range(1000):
        snapshot = client.get(f"/api/operations/{key}").json()
        if snapshot.get("finished"):
            break
        time.sleep(0.02)
    assert snapshot.get("state") == "SUCCEEDED", snapshot
    # sources unchanged
    assert world.plex.get_playlist(str(pa)).track_ids() == [str(a)]
    assert world.gonic.entries(gb_pl) == [gb]


def test_append_page_and_apply_via_web(client, world):
    import time

    _, ga = world.add_track("A/a.flac")
    _, gb = world.add_track("A/b.flac")
    src = world.gonic_playlist("Src", [ga, gb])
    dst = world.gonic_playlist("Dst", [ga])
    login(client)
    token = csrf(client, "/append")
    preview = client.post(
        "/append/preview",
        data={"csrf": token, "source_side": "gonic", "source_id": src, "dest_side": "gonic", "dest_id": dst},
    )
    assert preview.status_code == 200
    assert "Additions" in preview.text

    started = client.post(
        "/append/apply",
        data={"csrf": token, "source_side": "gonic", "source_id": src, "dest_side": "gonic", "dest_id": dst},
    )
    # Returns promptly with an operation id instead of holding the request open.
    assert started.status_code == 303
    assert started.headers["location"].startswith("/operations/")
    key = started.headers["location"].rsplit("/", 1)[-1]

    # Double submission reuses the same operation id (server-side dedupe).
    again = client.post(
        "/append/apply",
        data={"csrf": token, "source_side": "gonic", "source_id": src, "dest_side": "gonic", "dest_id": dst},
    )
    assert again.headers["location"] == started.headers["location"]

    snapshot = {}
    for _ in range(1000):
        response = client.get(f"/api/operations/{key}")
        assert response.status_code == 200
        snapshot = response.json()
        if snapshot.get("finished"):
            break
        time.sleep(0.02)
    assert snapshot.get("state") == "SUCCEEDED", snapshot
    assert snapshot.get("counters", {}).get("verified") is not None
    assert world.gonic.entries(dst) == [ga, gb]
    # the progress page renders and polls the same operation
    page = client.get(f"/operations/{key}")
    assert page.status_code == 200
    assert key in page.text


def test_operations_api_requires_login(client):
    assert client.get("/api/operations").status_code == 303


def test_append_requires_csrf(client):
    login(client)
    assert client.post("/append/preview", data={}).status_code == 403


def test_jobs_page_and_create_via_web(client, world):
    _, _ga = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Dst", [])
    login(client)
    token = csrf(client, "/jobs")
    created = client.post(
        "/jobs/create",
        data={
            "csrf": token,
            "type": "append",
            "name": "nightly-append",
            "params": f'{{"source_side":"gonic","source_id":"{gid}","dest_side":"gonic","dest_id":"{gid}"}}',
            "freq_kind": "hours",
            "freq_n": "6",
            "timezone": "UTC",
            "approved": "on",
        },
    )
    assert created.status_code == 303
    page = client.get("/jobs")
    assert "nightly-append" in page.text
    assert world.store.list_jobs()


def test_jobs_create_requires_csrf(client):
    login(client)
    assert client.post("/jobs/create", data={"type": "append"}).status_code == 403


def test_recurring_available_on_copy_merge_backup(client, world):
    a, _ = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    pa = world.plex_playlist("A", [a])
    gb_pl = world.gonic_playlist("B", [gb])
    login(client)
    token = csrf(client, "/migrate")
    # recurring is offered on the preview pages
    copy_preview = client.post(
        "/migrate/preview",
        data={"csrf": token, "source_side": "plex", "source_playlist_id": str(pa),
              "target_side": "gonic", "name": "Copied"},
    )
    assert copy_preview.status_code == 200
    assert 'name="type" value="copy"' in copy_preview.text

    merge_preview = client.post(
        "/merge/preview",
        data={"csrf": token, "source_a_side": "plex", "source_a_id": str(pa),
              "source_b_side": "gonic", "source_b_id": gb_pl, "target_side": "gonic",
              "name": "Merged", "order": "ab", "policy": "keep_all"},
    )
    assert merge_preview.status_code == 200
    assert 'name="type" value="merge"' in merge_preview.text
    # names-first preview shows the track label next to the id
    assert "Song" in merge_preview.text or "a.flac" in merge_preview.text

    world.settings.backups.directory = str(world.tmp_path / "bkrecur")
    backed = client.post("/backups/preview", data={"csrf": token, "selected": f"gonic:{gb_pl}"})
    assert backed.status_code == 200
    assert 'name="type" value="backup"' in backed.text


def test_merge_apply_runs_in_background(client, world):
    import time

    a, _ = world.add_track("A/a.flac")
    _b, gb = world.add_track("A/b.flac")
    pa = world.plex_playlist("A", [a])
    gb_pl = world.gonic_playlist("B", [gb])
    login(client)
    token = csrf(client, "/merge")
    started = client.post(
        "/merge/apply",
        data={"csrf": token, "source_a_side": "plex", "source_a_id": str(pa),
              "source_b_side": "gonic", "source_b_id": gb_pl, "target_side": "gonic",
              "name": "Merged BG", "order": "ab", "policy": "keep_all"},
    )
    assert started.status_code == 303
    key = started.headers["location"].rsplit("/", 1)[-1]
    snapshot = {}
    for _ in range(1000):
        snapshot = client.get(f"/api/operations/{key}").json()
        if snapshot.get("finished"):
            break
        time.sleep(0.02)
    assert snapshot.get("state") == "SUCCEEDED", snapshot
    assert snapshot.get("counters", {}).get("verified") is not None
    assert any(p.name == "Merged BG" for p in world.gonic.playlists.values())


def test_activity_filters(client, world):
    world.store.start_run("job:j1", "append", "schedule")
    login(client)
    page = client.get("/activity")
    assert page.status_code == 200
    assert "Showing" in page.text
    filtered = client.get("/activity?attention=1")
    assert filtered.status_code == 200
    assert "Needs attention only" in filtered.text


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


def test_append_preview_renders_readable_track_rows(client, world):
    a, ga = world.add_track("Metal/New.flac", title="New Song", artist="Metallica", album="Master")
    _b, gb = world.add_track("Other/Old.flac", title="Old", artist="Other")
    src = world.plex_playlist("Source", [a])
    dst = world.gonic_playlist("Dest", [gb])
    login(client)
    token = csrf(client, "/append")
    resp = client.post(
        "/append/preview",
        data={"csrf": token, "source_side": "plex", "source_id": str(src),
              "dest_side": "gonic", "dest_id": dst},
    )
    assert resp.status_code == 200
    text = resp.text
    assert "Tracks to add" in text
    # Readable name-first label, not the numeric destination id.
    assert "Metallica – New Song" in text
    # The stable id is kept for technical details only.
    assert "Track ID:" in text
    assert ga in text


def test_merge_preview_renders_readable_track_rows(client, world):
    a, _ = world.add_track("Artist/A.flac", title="A", artist="Artist")
    _b, gb = world.add_track("Artist/B.flac", title="B", artist="Artist")
    pa = world.plex_playlist("A", [a])
    gb_playlist = world.gonic_playlist("B", [gb])
    login(client)
    token = csrf(client, "/merge")
    resp = client.post(
        "/merge/preview",
        data={"csrf": token, "source_a_side": "plex", "source_a_id": str(pa),
              "source_b_side": "gonic", "source_b_id": gb_playlist, "target_side": "gonic",
              "name": "Merged", "order": "ab", "policy": "keep_all"},
    )
    assert resp.status_code == 200
    assert "Merged result" in resp.text
    assert "Artist – A" in resp.text
    assert "Artist – B" in resp.text


def test_copy_preview_renders_readable_track_rows(client, world):
    a, _ = world.add_track("Artist/Road.flac", title="Road", artist="Artist")
    pid = world.plex_playlist("Roadtrip", [a])
    login(client)
    token = csrf(client, "/migrate")
    resp = client.post(
        "/migrate/preview",
        data={"csrf": token, "source_side": "plex", "source_playlist_id": str(pid),
              "target_side": "gonic", "name": "Road copy"},
    )
    assert resp.status_code == 200
    assert "Tracks to copy" in resp.text
    assert "Artist – Road" in resp.text


def test_append_preview_reports_full_count_beyond_first_page(client, world):
    keys = [world.add_track(f"Bulk/t{i:03d}.flac", title=f"T{i:03d}", artist="Artist")[0] for i in range(60)]
    src = world.plex_playlist("Big", keys)
    dst = world.gonic_playlist("Empty", [])
    login(client)
    token = csrf(client, "/append")
    resp = client.post(
        "/append/preview",
        data={"csrf": token, "source_side": "plex", "source_id": str(src),
              "dest_side": "gonic", "dest_id": dst},
    )
    assert resp.status_code == 200
    assert "showing 50 of 60" in resp.text
    assert "Show 10 more" in resp.text



# --------------------------------------------------------------- contextual scheduling


def _append_schedule_data(dest_id, **overrides):
    data = {
        "csrf": overrides.pop("csrf", ""),
        "fmt": "json",
        "type": "append",
        "params": json.dumps({"source_side": "gonic", "source_id": dest_id,
                              "dest_side": "gonic", "dest_id": dest_id}),
        "config_revision": "rev-1",
        "submission_token": overrides.pop("token", "tok-1"),
        "name": overrides.pop("name", "Scheduled append"),
        "freq_kind": overrides.pop("freq_kind", "hours"),
        "freq_n": overrides.pop("freq_n", "6"),
        "timezone": overrides.pop("timezone", "UTC"),
        "run_mode": "every_run",
        "approved": "on",
    }
    data.update(overrides)
    return data


def test_workflows_offer_contextual_schedule(client, world):
    a, _ = world.add_track("Sched/a.flac")
    _b, gb = world.add_track("Sched/b.flac")
    pa = world.plex_playlist("Favorites", [a])
    gb_playlist = world.gonic_playlist("Road Trip", [gb])
    login(client)
    token = csrf(client, "/append")
    append_preview = client.post(
        "/append/preview",
        data={"csrf": token, "source_side": "plex", "source_id": str(pa),
              "dest_side": "gonic", "dest_id": gb_playlist},
    )
    assert append_preview.status_code == 200
    text = append_preview.text
    assert "Schedule…" in text
    assert 'name="type" value="append"' in text
    assert "Add missing tracks from" in text and "Favorites" in text
    assert "config_revision" in text


def test_mapping_detail_offers_sync_schedule(client, world):
    from playlist_sync.models import Direction

    _a, ga = world.add_track("Sched/a.flac")
    pid = world.plex_playlist("Favorites", [1])
    gid = world.gonic_playlist("Road Trip", [ga])
    world.store.create_mapping(
        name="m", direction=Direction.PLEX_TO_GONIC.value,
        plex_playlist_id=str(pid), gonic_playlist_id=gid,
    )
    login(client)
    text = client.get(f"/mappings/{world.store.list_mappings()[0].id}").text
    assert "Schedule…" in text
    assert 'name="type" value="sync"' in text


def test_schedule_endpoint_creates_job_and_persists(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    gid = world.gonic_playlist("Sched Dst", [ga])
    login(client)
    data = _append_schedule_data(gid, csrf=csrf(client, "/jobs"))
    resp = client.post("/jobs/schedule", data=data)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["next_run"]
    assert "Scheduled" in payload["message"]
    jobs = world.store.list_jobs()
    assert len(jobs) == 1 and jobs[0]["name"] == "Scheduled append"
    # survives a fresh app instance backed by the same store
    app2 = create_app(world.settings, ctx=world.ctx)
    with TestClient(app2, follow_redirects=False) as c2:
        login(c2)
        assert "Scheduled append" in c2.get("/jobs").text


def test_schedule_rejects_incomplete_operation(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    gid = world.gonic_playlist("Dst", [ga])
    login(client)
    data = _append_schedule_data(gid, csrf=csrf(client, "/jobs"))
    data["params"] = json.dumps({"source_side": "gonic", "source_id": gid})  # no destination
    resp = client.post("/jobs/schedule", data=data)
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert not world.store.list_jobs()


def test_schedule_requires_reviewed_revision(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    gid = world.gonic_playlist("Dst", [ga])
    login(client)
    data = _append_schedule_data(gid, csrf=csrf(client, "/jobs"))
    data["config_revision"] = ""
    resp = client.post("/jobs/schedule", data=data)
    assert resp.status_code == 400
    assert not world.store.list_jobs()


def test_schedule_duplicate_warns_then_override(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    gid = world.gonic_playlist("Dst", [ga])
    login(client)
    token = csrf(client, "/jobs")
    first = client.post("/jobs/schedule", data=_append_schedule_data(gid, csrf=token, token="t1"))
    assert first.status_code == 200 and first.json()["ok"]
    # an identical schedule with a new submission token is refused, not silently duplicated
    duplicate = client.post(
        "/jobs/schedule", data=_append_schedule_data(gid, csrf=token, token="t2", name="Second")
    )
    assert duplicate.status_code == 400
    assert "identical active schedule" in duplicate.json()["error"]
    assert len(world.store.list_jobs()) == 1
    # an intentional separate schedule is allowed
    override = client.post(
        "/jobs/schedule",
        data=_append_schedule_data(gid, csrf=token, token="t3", name="Second", allow_duplicate="on"),
    )
    assert override.status_code == 200 and override.json()["ok"]
    assert len(world.store.list_jobs()) == 2


def test_schedule_double_submit_is_idempotent(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    gid = world.gonic_playlist("Dst", [ga])
    login(client)
    data = _append_schedule_data(gid, csrf=csrf(client, "/jobs"), token="same-token")
    first = client.post("/jobs/schedule", data=data)
    assert first.json()["ok"]
    second = client.post("/jobs/schedule", data=data)
    assert second.status_code == 200
    assert second.json()["ok"]
    assert "already saved" in second.json()["message"]
    assert len(world.store.list_jobs()) == 1


def test_schedule_also_run_now_runs_exactly_once(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    _b, gb = world.add_track("Sched/b.flac")
    source = world.gonic_playlist("Source", [ga, gb])
    dest = world.gonic_playlist("Dest", [])
    login(client)
    data = _append_schedule_data(dest, csrf=csrf(client, "/jobs"), token="run-now", also_run_now="on")
    data["params"] = json.dumps({"source_side": "gonic", "source_id": source,
                                 "dest_side": "gonic", "dest_id": dest})
    resp = client.post("/jobs/schedule", data=data)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] and "ran" in resp.json()
    assert len(world.store.list_jobs()) == 1
    runs = [r for r in world.store.list_runs(limit=50) if r["trigger"] == "schedule"]
    assert len(runs) == 1
    # resubmitting the same form does not run again
    client.post("/jobs/schedule", data=data)
    runs = [r for r in world.store.list_runs(limit=50) if r["trigger"] == "schedule"]
    assert len(runs) == 1


def test_schedule_requires_csrf(client):
    login(client)
    assert client.post("/jobs/schedule", data={"type": "append"}).status_code == 403


def test_schedule_invalid_frequency_is_rejected(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    gid = world.gonic_playlist("Dst", [ga])
    login(client)
    data = _append_schedule_data(gid, csrf=csrf(client, "/jobs"))
    data["freq_kind"] = "minutes"
    data["freq_n"] = "1"  # below the minimum
    resp = client.post("/jobs/schedule", data=data)
    assert resp.status_code == 400
    assert not world.store.list_jobs()


def test_jobs_edit_operation_link_reopens_prefilled_workflow(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    gid = world.gonic_playlist("Prefill Dst", [ga])
    login(client)
    created = client.post("/jobs/schedule", data=_append_schedule_data(gid, csrf=csrf(client, "/jobs")))
    assert created.json()["ok"]
    page = client.get("/jobs")
    assert "Edit operation" in page.text
    assert "/append?" in page.text
    # reopening the workflow prefills the configured operation
    reopened = client.get(f"/append?source_side=gonic&source_id={gid}&dest_side=gonic&dest_id={gid}")
    assert reopened.status_code == 200
    assert f'value="{gid}"' in reopened.text


def test_jobs_edit_schedule_updates_frequency(client, world):
    _a, ga = world.add_track("Sched/a.flac")
    gid = world.gonic_playlist("Dst", [ga])
    login(client)
    token = csrf(client, "/jobs")
    client.post("/jobs/schedule", data=_append_schedule_data(gid, csrf=token))
    job = world.store.list_jobs()[0]
    edited = client.post(
        f"/jobs/{job['id']}/edit",
        data={"csrf": token, "name": "renamed", "freq_kind": "hours", "freq_n": "12",
              "timezone": "UTC", "run_mode": "every_run", "approved": "on"},
    )
    assert edited.status_code == 303
    updated = world.store.get_job(job["id"])
    assert updated["name"] == "renamed"
    assert updated["frequency"]["n"] == 12


def test_append_preview_escapes_malicious_metadata(client, world):
    a, _ = world.add_track(
        "Meta/x.flac", title="<script>alert(1)</script>", artist="<b>bad</b>"
    )
    _b, gb = world.add_track("Meta/keep.flac", title="Keep", artist="Artist")
    src = world.plex_playlist("Source", [a])
    dst = world.gonic_playlist("Dest", [gb])
    login(client)
    token = csrf(client, "/append")
    resp = client.post(
        "/append/preview",
        data={"csrf": token, "source_side": "plex", "source_id": str(src),
              "dest_side": "gonic", "dest_id": dst},
    )
    assert resp.status_code == 200
    assert "<script>alert(1)" not in resp.text
    assert "&lt;script&gt;alert(1)" in resp.text


def test_append_preview_falls_back_to_filename_when_title_missing(client, world):
    _a, gid = world.add_track(
        "Meta/goniconly.flac", title=" ", artist=" ", album=" ", gonic_only=True
    )
    src = world.gonic_playlist("Source", [gid])
    dst = world.gonic_playlist("Dest", [])
    login(client)
    token = csrf(client, "/append")
    resp = client.post(
        "/append/preview",
        data={"csrf": token, "source_side": "gonic", "source_id": src,
              "dest_side": "gonic", "dest_id": dst},
    )
    assert resp.status_code == 200
    assert "goniconly.flac" in resp.text
    assert "filename shown because title metadata is unavailable" in resp.text.lower()


def test_mapping_preview_renders_readable_additions(client, world):
    from playlist_sync.models import Direction

    a, ga = world.add_track("M/A.flac", title="A", artist="Artist")
    b, gb = world.add_track("M/B.flac", title="B", artist="Artist")
    pid = world.plex_playlist("Src", [a, b])
    gid = world.gonic_playlist("Dst", [ga])  # gb is missing -> an addition
    mapping = world.store.create_mapping(
        name="m", direction=Direction.PLEX_TO_GONIC.value,
        plex_playlist_id=str(pid), gonic_playlist_id=gid,
    )
    login(client)
    token = csrf(client, f"/mappings/{mapping.id}")
    resp = client.post(f"/mappings/{mapping.id}/preview", data={"csrf": token})
    assert resp.status_code == 200
    # readable row, not a bare numeric id
    assert "Artist – B" in resp.text
    assert '<span class="track-primary">Artist – B</span>' in resp.text
    assert f'<td class="diff-add">{gb}<br>' not in resp.text


def test_backup_schedule_edit_operation_prefills_selection(client, world):
    _a, ga = world.add_track("B/a.flac")
    gid = world.gonic_playlist("Backup Me", [ga])
    login(client)
    token = csrf(client, "/backups")
    resp = client.post("/jobs/schedule", data={
        "csrf": token, "fmt": "json", "type": "backup",
        "params": json.dumps({"selected": [["gonic", gid]], "allow_partial": False}),
        "config_revision": "rev-b", "submission_token": "b1", "name": "Backup nightly",
        "freq_kind": "daily", "freq_time": "03:00", "timezone": "UTC",
        "run_mode": "every_run", "approved": "on",
    })
    assert resp.status_code == 200 and resp.json()["ok"]
    assert "Edit operation" in client.get("/jobs").text
    # reopening the backups workflow preselects the saved playlist
    reopened = client.get(f"/backups?selected=gonic:{gid}")
    assert reopened.status_code == 200
    assert f'"{("gonic:" + gid)!s}"' in reopened.text
