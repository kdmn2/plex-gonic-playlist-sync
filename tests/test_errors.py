
from playlist_sync.backends import BackendAuthError, BackendUnavailable, PlaylistNotFound
from playlist_sync.errors import (
    AppError,
    ErrorCategory,
    Outcome,
    Retryability,
    format_traceback,
    make_error,
    wrap_exception,
)


def test_error_carries_stable_code_and_classification():
    err = make_error("CONNECT_TIMEOUT", "timed out")
    assert err.code == "CONNECT_TIMEOUT"
    assert err.category is ErrorCategory.CONNECTIVITY
    assert err.retryability is Retryability.TRANSIENT
    assert err.outcome is Outcome.NOT_STARTED
    assert err.action  # suggested next action is populated from the spec
    assert err.http_status == 504


def test_backend_errors_are_typed_app_errors():
    assert isinstance(BackendAuthError("x"), AppError)
    assert BackendAuthError("x").code == "HTTP_AUTH"
    assert BackendUnavailable("x").retryability is Retryability.TRANSIENT
    assert PlaylistNotFound("x").code == "PLAYLIST_NOT_FOUND"


def test_wrap_exception_preserves_code_and_redacts_details():
    from playlist_sync.redact import register_secret

    register_secret("supersecret")
    err = wrap_exception(RuntimeError("boom supersecret"))
    assert err.code == "INTERNAL"
    export = err.to_dict(include_details=True)
    assert "supersecret" not in export["details"]["technical"]


def test_app_error_str_is_safe():
    err = make_error("MUTATION_OUTCOME_UNKNOWN", "The result is not yet known.")
    assert str(err) == "The result is not yet known."


def test_outcome_and_retry_distinctions():
    assert make_error("CONCURRENT_EDIT", "x").outcome is Outcome.FAILED_SAFE
    assert make_error("MUTATION_OUTCOME_UNKNOWN", "x").outcome is Outcome.OUTCOME_UNKNOWN
    assert make_error("MUTATION_PARTIAL", "x").outcome is Outcome.PARTIALLY_APPLIED
    assert make_error("CONFLICT_DETECTED", "x").outcome is Outcome.CONFLICT
    assert make_error("HTTP_AUTH", "x").retryability is Retryability.NEVER


def test_format_traceback_is_scrubbed():
    from playlist_sync.redact import register_secret

    register_secret("tracetoken")
    try:
        raise ValueError("contains tracetoken here")
    except ValueError as exc:
        text = format_traceback(exc)
    assert "tracetoken" not in text
