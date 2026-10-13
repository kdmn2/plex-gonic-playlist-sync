import logging

import httpx
import pytest

from playlist_sync.backends import GonicClient
from playlist_sync.backends.resilience import OutageLogger, parse_retry_after
from playlist_sync.errors import AppError


def make_client(handler, **kwargs) -> GonicClient:
    kwargs.setdefault("retry_attempts", 3)
    kwargs.setdefault("retry_base_seconds", 0.01)
    kwargs.setdefault("retry_max_seconds", 0.05)
    kwargs.setdefault("retry_total_seconds", 1.0)
    return GonicClient(
        "http://gonic.test", "admin", "secret", transport=httpx.MockTransport(handler), **kwargs
    )


def ok(**body):
    return httpx.Response(200, json={"subsonic-response": {"status": "ok", "version": "1.16.1", **body}})


def test_reads_retry_on_transient_connection_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return ok()

    client = make_client(handler)
    root = client._request("ping")
    assert root["status"] == "ok"
    assert calls["n"] == 2


def test_mutations_are_never_auto_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    client = make_client(handler)
    with pytest.raises(AppError) as excinfo:
        client.create_playlist("Mix", ["tr-1"])
    assert excinfo.value.retryability.value == "transient"
    assert calls["n"] == 1  # exactly one attempt for a mutation


def test_circuit_breaker_suppresses_after_threshold():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    client = make_client(
        handler, retry_attempts=1, circuit_failure_threshold=2, circuit_reset_seconds=60
    )
    for _ in range(2):
        with pytest.raises(AppError):
            client._request("ping")
    after = calls["n"]
    with pytest.raises(AppError) as excinfo:
        client._request("ping")
    assert excinfo.value.code == "BACKEND_UNAVAILABLE"
    assert calls["n"] == after  # no request reached the transport


def test_retry_after_header_is_honoured():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return ok()

    client = make_client(handler)
    assert client._request("ping")["status"] == "ok"
    assert calls["n"] == 2


def test_parse_retry_after_forms():
    assert parse_retry_after("5") == 5
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0


def test_html_login_page_is_detected():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>login</html>")

    client = make_client(handler, retry_attempts=1)
    with pytest.raises(AppError) as excinfo:
        client._request("ping")
    assert excinfo.value.code == "RESPONSE_NOT_API"


def test_malformed_json_is_detected():
    def handler(request):
        return httpx.Response(200, text="{not json")

    client = make_client(handler, retry_attempts=1)
    with pytest.raises(AppError) as excinfo:
        client._request("ping")
    assert excinfo.value.code == "RESPONSE_MALFORMED"


def test_oversized_response_is_rejected():
    def handler(request):
        return ok(payload="x" * 5000)

    client = make_client(handler, retry_attempts=1, max_response_bytes=100)
    with pytest.raises(AppError) as excinfo:
        client._request("ping")
    assert excinfo.value.code == "RESPONSE_TOO_LARGE"


def test_http_5xx_is_classified_transient():
    def handler(request):
        return httpx.Response(503, text="maintenance")

    client = make_client(handler, retry_attempts=1)
    with pytest.raises(AppError) as excinfo:
        client._request("ping")
    assert excinfo.value.code == "HTTP_MAINTENANCE"


def test_subsonic_error_envelope_on_http_200():
    def handler(request):
        return httpx.Response(
            200,
            json={"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "bad"}}},
        )

    client = make_client(handler, retry_attempts=1)
    with pytest.raises(AppError) as excinfo:
        client._request("ping")
    assert excinfo.value.code == "HTTP_AUTH"


def test_auth_failure_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "bad"}}},
        )

    client = make_client(handler)
    with pytest.raises(AppError):
        client._request("ping")
    assert calls["n"] == 1


def test_outage_logger_deduplicates(caplog):
    logger = OutageLogger("gonic", window_seconds=999)
    with caplog.at_level(logging.WARNING, logger="playlist_sync.resilience"):
        logger.outage("CONNECT_TIMEOUT", "down")
        logger.outage("CONNECT_TIMEOUT", "down")
        logger.outage("CONNECT_TIMEOUT", "down")
    messages = [r for r in caplog.records if "down" in r.getMessage()]
    assert len(messages) == 1
    with caplog.at_level(logging.INFO, logger="playlist_sync.resilience"):
        logger.recovered()
    assert any("recovered" in r.getMessage() for r in caplog.records)
