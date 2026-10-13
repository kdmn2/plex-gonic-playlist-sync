import time

import httpx

from playlist_sync import plex_auth


def pins_handler(*, authorize_after=0, status_code=200, deny=False, expire=False):
    calls = {"n": 0}

    def handler(request):
        if request.method == "POST" and request.url.path == "/api/v2/pins":
            return httpx.Response(201, json={"id": 1, "code": "ABCD"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/api/v2/pins/1":
            calls["n"] += 1
            if expire:
                return httpx.Response(404, json={})
            if deny:
                return httpx.Response(401, json={})
            if calls["n"] > authorize_after:
                return httpx.Response(200, json={"id": 1, "authToken": "valid-token-abcdef123456"})
            return httpx.Response(200, json={"id": 1})

    return handler


def make_auth(handler, **kwargs):
    return plex_auth.PlexPinAuth("client-id-test", transport=httpx.MockTransport(handler), **kwargs)


def test_create_and_poll_authorized():
    auth = make_auth(pins_handler(authorize_after=1))
    flow = auth.create()
    assert flow.code == "ABCD"
    assert auth.poll(flow) == "pending"
    flow.last_poll = 0  # bypass interval
    assert auth.poll(flow) == "authorized"
    assert flow.token == "valid-token-abcdef123456"


def test_poll_expired_by_ttl():
    auth = make_auth(pins_handler(), ttl=10)
    flow = auth.create()
    flow.created = time.monotonic() - 11
    assert auth.poll(flow) == "expired"


def test_poll_404_is_expired():
    auth = make_auth(pins_handler(expire=True))
    flow = auth.create()
    assert auth.poll(flow) == "expired"


def test_poll_denied():
    auth = make_auth(pins_handler(deny=True))
    flow = auth.create()
    assert auth.poll(flow) == "denied"


def test_cancel_clears_token():
    auth = make_auth(pins_handler())
    flow = auth.create()
    flow.token = "x"
    auth.cancel(flow)
    assert flow.status == "cancelled"
    assert flow.token is None


def _server_handler(*, home_status=200, sections_status=200, redirect=False):
    def handler(request):
        if request.url.path == "/":
            if redirect:
                return httpx.Response(302, headers={"Location": "https://evil.example/"})
            if home_status != 200:
                return httpx.Response(home_status, json={})
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "friendlyName": "MyPlex",
                        "machineIdentifier": "machine-123",
                        "version": "1.40.0",
                    }
                },
            )
        if request.url.path == "/library/sections":
            if sections_status != 200:
                return httpx.Response(sections_status, json={})
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "Directory": [
                            {"key": "1", "title": "Music", "type": "artist"},
                            {"key": "2", "title": "Movies", "type": "movie"},
                        ]
                    }
                },
            )
        return httpx.Response(404)

    return handler


def test_validate_token_success_and_no_token_leak():
    result = plex_auth.validate_token(
        "http://plex.test", "tok-1234567890abcdef", transport=httpx.MockTransport(_server_handler())
    )
    assert result.ok
    assert result.server_name == "MyPlex"
    assert result.server_identity == "machine-123"
    assert [s["title"] for s in result.music_sections] == ["Music"]
    assert "tok-1234567890abcdef" not in str(result.public())


def test_validate_token_rejects_invalid():
    result = plex_auth.validate_token(
        "http://plex.test", "bad", transport=httpx.MockTransport(_server_handler(home_status=401))
    )
    assert not result.ok
    assert "rejected" in result.reason.lower()


def test_validate_token_refuses_redirect():
    result = plex_auth.validate_token(
        "http://plex.test", "tok-1234567890abcdef", transport=httpx.MockTransport(_server_handler(redirect=True))
    )
    assert not result.ok
    assert result.redirect_blocked is True


def test_validate_token_rejects_bad_url():
    result = plex_auth.validate_token("not-a-url", "tok")
    assert not result.ok
