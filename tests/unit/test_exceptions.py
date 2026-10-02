"""Exception hierarchy and driver-error mapping (ported from friendly_connection_error tests)."""

from __future__ import annotations

import builtins

import pytest

from enterprise_connectors.core import exceptions as errors


def test_hierarchy_matches_spec() -> None:
    for cls in (
        errors.ConfigurationError,
        errors.AuthenticationError,
        errors.AuthorizationError,
        errors.ConnectionError,
        errors.ConnectionTimeout,
        errors.ConnectionPoolError,
        errors.UnsupportedOperation,
    ):
        assert issubclass(cls, errors.ConnectorError)
    assert issubclass(errors.PoolExhausted, errors.ConnectionPoolError)
    assert issubclass(errors.PoolTimeout, errors.ConnectionPoolError)


def test_builtin_compatibility() -> None:
    assert issubclass(errors.ConnectionError, builtins.ConnectionError)
    assert issubclass(errors.ConnectionTimeout, TimeoutError)
    assert issubclass(errors.PoolTimeout, TimeoutError)
    err = errors.ConnectionError("boom", connector_type="pg")
    assert str(err) == "boom" and err.connector_type == "pg"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("An error occurred (403) when calling the HeadBucket operation: Forbidden", errors.AuthorizationError),
        ('FATAL: password authentication failed for user "bob"', errors.AuthenticationError),
        ("Unable to locate credentials", errors.AuthenticationError),
        ("database 'foo' does not exist", errors.ConfigurationError),
        ("Connection timed out", errors.ConnectionTimeout),
        ("could not translate host name 'bogus' to address", errors.ConnectionError),
        ("some totally novel driver failure", errors.ConnectionError),
    ],
)
def test_map_driver_error_classifies(message: str, expected: type[errors.ConnectorError]) -> None:
    mapped = errors.map_driver_error(RuntimeError(message), "postgres")
    assert type(mapped) is expected
    assert mapped.connector_type == "postgres"
    assert mapped.driver_error == "RuntimeError"


def test_builtin_timeout_maps_to_connection_timeout() -> None:
    assert isinstance(errors.map_driver_error(TimeoutError(), "x"), errors.ConnectionTimeout)


def test_connector_errors_pass_through_unchanged() -> None:
    original = errors.PoolExhausted("full")
    assert errors.map_driver_error(original, "x") is original


def test_unrecognized_error_keeps_redacted_detail() -> None:
    mapped = errors.map_driver_error(RuntimeError("weird failure dsn=host=db password=hunter2"), "postgres")
    assert "weird failure" in str(mapped)
    assert "hunter2" not in str(mapped)


def test_detail_is_single_line_and_bounded() -> None:
    mapped = errors.map_driver_error(RuntimeError("x" * 1000 + "\nsecond line"), "pg")
    assert mapped.detail is not None
    assert "second line" not in mapped.detail and len(mapped.detail) <= 200
