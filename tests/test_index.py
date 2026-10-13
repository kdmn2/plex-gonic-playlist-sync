import pytest
from fastapi.testclient import TestClient

from playlist_sync.models import ServerKind
from playlist_sync.web.app import create_app


@pytest.fixture
def client(world):
    world.settings.ui.password = "ui-pass"
    world.settings.ui.username = "ui-user"
    world.settings.ui.session_secret = "s"
    world.settings.sync.poll_seconds = 0
    world.settings.limits.warm_indexes = False  # avoid background activity in tests
    app = create_app(world.settings, ctx=world.ctx)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client):
    assert client.post("/login", data={"username": "ui-user", "password": "ui-pass"}).status_code == 303


def test_build_index_updates_status(world):
    world.add_track("A/a.flac")
    index = world.ctx.build_index(ServerKind.GONIC)
    assert index.size == 1
    snapshot = world.ctx.index_status_snapshot()
    assert snapshot["gonic"]["state"] == "ready"
    assert snapshot["gonic"]["tracks"] == 1
    assert snapshot["gonic"]["cached"] is True
    assert snapshot["plex"]["state"] == "idle"


def test_invalidate_marks_index_idle(world):
    world.add_track("A/a.flac")
    world.ctx.build_index(ServerKind.GONIC)
    world.ctx.invalidate_index(ServerKind.GONIC)
    assert world.ctx.index_status_snapshot()["gonic"]["state"] == "idle"
    assert world.ctx.index_status_snapshot()["gonic"]["cached"] is False


def test_index_build_failure_records_error(world):
    def boom():
        raise RuntimeError("nope")

    world.gonic_client.iter_library_tracks = boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        world.ctx.build_index(ServerKind.GONIC)
    snapshot = world.ctx.index_status_snapshot()
    assert snapshot["gonic"]["state"] == "error"
    assert snapshot["gonic"]["error"] == "RuntimeError"


async def test_index_warmer_builds_both_indexes(world, monkeypatch):
    from playlist_sync.web.warmup import IndexWarmer

    world.add_track("A/a.flac")
    world.settings.limits.index_refresh_seconds = 0  # single pass, then stop

    async def no_wait(self, seconds):
        return False

    monkeypatch.setattr(IndexWarmer, "_sleep", no_wait)
    warmer = IndexWarmer(world.ctx)
    await warmer._run()
    assert "plex" in world.ctx.index_cache
    assert "gonic" in world.ctx.index_cache


def test_index_status_endpoint(client, world):
    world.add_track("A/a.flac")
    world.ctx.build_index(ServerKind.GONIC)
    login(client)
    response = client.get("/api/index-status")
    assert response.status_code == 200
    body = response.json()
    assert body["gonic"]["state"] == "ready"
    assert set(body) >= {"plex", "gonic"}


def test_index_status_requires_login(client):
    assert client.get("/api/index-status").status_code == 303
