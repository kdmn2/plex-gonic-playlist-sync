import re

import pytest
from fastapi.testclient import TestClient

from playlist_sync.web.app import create_app


@pytest.fixture
def client(world):
    world.settings.ui.password = "ui-pass"
    world.settings.ui.username = "ui-user"
    world.settings.ui.session_secret = "test-session-secret"
    world.settings.sync.poll_seconds = 0
    app = create_app(world.settings, ctx=world.ctx)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client):
    response = client.post("/login", data={"username": "ui-user", "password": "ui-pass"})
    assert response.status_code == 303
    return response


def csrf_from(client, path="/mappings/new"):
    html = client.get(path).text
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "csrf token not found"
    return match.group(1)


def test_unauthenticated_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_healthz_is_open(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_wrong_credentials_rejected(client):
    response = client.post("/login", data={"username": "ui-user", "password": "nope"})
    assert response.status_code == 401


def test_login_then_dashboard(client):
    login(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "Mappings" in response.text


def test_mutation_requires_csrf(client):
    login(client)
    response = client.post("/mappings", data={"name": "x", "direction": "plex_to_gonic"})
    assert response.status_code == 403


def test_mutation_with_csrf_creates_mapping(client, world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [ga])
    login(client)
    token = csrf_from(client)
    response = client.post(
        "/mappings",
        data={
            "csrf": token,
            "name": "Web Mapping",
            "direction": "plex_to_gonic",
            "plex_playlist_id": str(pid),
            "gonic_playlist_id": gid,
        },
    )
    assert response.status_code == 303
    assert any(m.name == "Web Mapping" for m in world.store.list_mappings())


def test_no_secrets_in_any_page(client, world):
    login(client)
    secrets = [world.settings.plex.token, world.settings.gonic.password, "ui-pass"]
    for path in ["/", "/mappings/new", "/playlists?server=plex", "/playlists?server=gonic"]:
        text = client.get(path).text
        for secret in secrets:
            assert secret not in text


def test_serve_requires_ui_password(world):
    world.settings.ui.password = ""
    with pytest.raises(RuntimeError):
        create_app(world.settings, ctx=world.ctx)
