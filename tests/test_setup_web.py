import re

import httpx
import pytest
from fastapi.testclient import TestClient

from playlist_sync import plex_auth, secretstore
from playlist_sync.web.app import create_app

TOKEN = "valid-token-abcdef123456"


def _pins_transport():
    def handler(request):
        if request.method == "POST" and request.url.path == "/api/v2/pins":
            return httpx.Response(201, json={"id": 7, "code": "WXYZ"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/api/v2/pins/7":
            return httpx.Response(200, json={"id": 7, "authToken": TOKEN})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


@pytest.fixture
def setup_client(world):
    world.settings.ui.password = "ui-pass"
    world.settings.ui.username = "ui-user"
    world.settings.ui.session_secret = "test-secret"
    world.settings.sync.poll_seconds = 0
    world.settings.plex.token = ""
    world.settings.plex.token_file = None

    app = create_app(world.settings, ctx=world.ctx)
    app.state.plex_pin_factory = lambda cid: plex_auth.PlexPinAuth(
        cid, transport=_pins_transport()
    )
    app.state.plex_validator = lambda base, token: plex_auth.ValidationResult(
        True, server_name="PlexHome", server_identity="m-1", music_sections=[{"key": "1", "title": "Music"}]
    )
    with TestClient(app, follow_redirects=False) as client:
        yield client


def login(client):
    assert client.post("/login", data={"username": "ui-user", "password": "ui-pass"}).status_code == 303


def csrf(client, path="/setup/plex"):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match
    return match.group(1)


def test_setup_requires_login(setup_client):
    response = setup_client.get("/setup/plex")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_start_requires_csrf(setup_client):
    login(setup_client)
    response = setup_client.post("/setup/plex/start")
    assert response.status_code == 403


def test_full_connect_flow_stores_token_server_side(setup_client, world):
    login(setup_client)
    token = csrf(setup_client)
    started = setup_client.post("/setup/plex/start", data={"csrf": token})
    assert started.status_code == 303

    # Polling the page authorizes, validates, and stores the token server-side.
    page = setup_client.get("/setup/plex")
    assert page.status_code == 303
    assert "connected" in page.headers["location"]

    path = secretstore.managed_token_path(world.settings.config_dir)
    assert path.exists()
    assert path.read_text().strip() == TOKEN

    # The token must never appear in any browser response.
    for endpoint in ["/setup/plex", "/status", "/"]:
        text = setup_client.get(endpoint).text
        assert TOKEN not in text

    # Credential source is now UI-managed.
    assert "ui-managed" in setup_client.get("/setup/plex").text


def test_setup_responses_are_not_cached(setup_client):
    login(setup_client)
    response = setup_client.get("/setup/plex")
    assert "no-store" in response.headers.get("cache-control", "")


def test_external_token_takes_precedence_over_ui(setup_client, world):
    world.settings.plex.token = "external-token-xyz"
    login(setup_client)
    token = csrf(setup_client)
    response = setup_client.post("/setup/plex/start", data={"csrf": token})
    assert response.status_code == 303
    assert "externally" in response.headers["location"] or "error" in response.headers["location"]


def test_forget_keeps_mappings_and_history(setup_client, world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = world.store.create_mapping(
        name="keep-me", direction="plex_to_gonic", plex_playlist_id=str(pid), gonic_playlist_id=gid
    )
    secretstore.write_managed_token(world.settings.config_dir, TOKEN)

    login(setup_client)
    token = csrf(setup_client)
    response = setup_client.post("/setup/plex/forget", data={"csrf": token, "confirm": "yes"})
    assert response.status_code == 303
    assert not secretstore.has_managed_token(world.settings.config_dir)
    # mappings and history are untouched
    assert world.store.get_mapping(mapping.id) is not None


def test_forget_requires_confirmation(setup_client, world):
    secretstore.write_managed_token(world.settings.config_dir, TOKEN)
    login(setup_client)
    token = csrf(setup_client)
    setup_client.post("/setup/plex/forget", data={"csrf": token})
    assert secretstore.has_managed_token(world.settings.config_dir)  # still there


def test_validation_failure_does_not_store(setup_client, world):
    login(setup_client)
    setup_client.app.state.plex_validator = lambda base, token: plex_auth.ValidationResult(
        False, "Plex rejected the token"
    )
    token = csrf(setup_client)
    setup_client.post("/setup/plex/start", data={"csrf": token})
    page = setup_client.get("/setup/plex")
    assert page.status_code == 200
    assert "rejected" in page.text.lower()
    assert not secretstore.has_managed_token(world.settings.config_dir)
