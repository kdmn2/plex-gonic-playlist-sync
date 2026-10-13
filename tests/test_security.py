import logging

from playlist_sync.logging_setup import QueryStripFilter
from playlist_sync.redact import RedactingFilter, Secret, register_secret, scrub


def test_secret_repr_and_str_do_not_leak():
    secret = Secret("super-secret-value")
    assert "super-secret-value" not in repr(secret)
    assert "super-secret-value" not in str(secret)
    assert secret.get() == "super-secret-value"


def test_scrub_removes_registered_secrets():
    register_secret("a1b2c3d4e5zz")
    assert scrub("token=a1b2c3d4e5zz end") == "token=*** end"


def test_redacting_log_filter():
    register_secret("hunter2xyz")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "pass hunter2xyz", None, None)
    RedactingFilter().filter(record)
    assert "hunter2xyz" not in record.getMessage()


def test_query_strip_filter():
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: GET http://gonic/rest/ping?u=admin&t=deadbeef&s=abc123",
        None,
        None,
    )
    QueryStripFilter().filter(record)
    assert "deadbeef" not in record.getMessage()
    assert "abc123" not in record.getMessage()


def test_gonic_auth_error_does_not_include_password(world):
    from playlist_sync.backends import BackendAuthError, GonicClient

    client = GonicClient("http://gonic.test", "admin", "my-secret-pw", transport=world.gonic.transport())
    try:
        client.list_playlists()
    except BackendAuthError as exc:
        assert "my-secret-pw" not in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected auth failure")
