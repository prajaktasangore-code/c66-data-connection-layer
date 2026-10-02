"""SDK exception hierarchy rooted at ConnectorError.

Messages never contain secret material: driver errors are classified by
``map_driver_error`` and only a redacted one-line detail is kept.
"""

from __future__ import annotations

import builtins

from ..utils.logging import redact

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "ConnectionError",
    "ConnectionPoolError",
    "ConnectionTimeout",
    "ConnectorError",
    "PoolExhausted",
    "PoolTimeout",
    "UnsupportedOperation",
    "map_driver_error",
]

_DETAIL_MAX_CHARS = 200


class ConnectorError(Exception):
    """Base class for every error raised by the SDK."""

    def __init__(
        self,
        message: str,
        *,
        connector_type: str | None = None,
        detail: str | None = None,
        driver_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.connector_type = connector_type
        self.detail = detail
        self.driver_error = driver_error

    def __str__(self) -> str:
        return f"{self.message} ({self.detail})" if self.detail else self.message


class ConfigurationError(ConnectorError):
    """Invalid or incomplete connection configuration, or a missing optional dependency."""


class AuthenticationError(ConnectorError):
    """Credentials are missing, invalid, expired or could not be obtained."""


class AuthorizationError(ConnectorError):
    """Authenticated, but not permitted to access the source."""


class ConnectionError(ConnectorError, builtins.ConnectionError):  # noqa: A001 - name fixed by spec
    """The source could not be reached or the connection failed. Also a builtin ConnectionError."""


class ConnectionTimeout(ConnectorError, TimeoutError):
    """Establishing or checking a connection timed out. Also a builtin TimeoutError."""


class ConnectionPoolError(ConnectorError):
    """Pool-level failure (e.g. using a closed pool)."""


class PoolExhausted(ConnectionPoolError):
    """No resource available and the pool is configured to fail fast."""


class PoolTimeout(ConnectionPoolError, TimeoutError):
    """No resource became available within the checkout timeout."""


class UnsupportedOperation(ConnectorError):
    """The connector does not support the requested operation or credential type."""


_AUTHENTICATION = (
    "401",
    "unauthorized",
    "password authentication failed",
    "authentication failed",
    "invalid credentials",
    "invalidaccesskeyid",
    "signaturedoesnotmatch",
    "no password supplied",
    "unable to locate credentials",
    "no credentials",
    "credentials not found",
    "could not find aws credentials",
    "invalid_grant",
    "invalid_client",
    "token expired",
    "expired token",
)
_AUTHORIZATION = (
    "403",
    "forbidden",
    "accessdenied",
    "access denied",
    "not authorized",
    "permission denied",
    "insufficient privilege",
)
_NOT_FOUND = ("404", "nosuchbucket", "does not exist", "unknown database", "not found")
_TIMEOUT = ("timed out", "timeout")
_UNREACHABLE = (
    "connection refused",
    "failed to establish a new connection",
    "could not connect",
    "name or service not known",
    "getaddrinfo failed",
    "could not translate host name",
    "nodename nor servname",
    "no such host",
    "server closed the connection",
    "network is unreachable",
)


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else ""


def map_driver_error(exc: BaseException, connector_type: str) -> ConnectorError:
    """Translate a driver/library exception into the SDK hierarchy.

    Callers should ``raise map_driver_error(e, name) from None`` so the raw driver
    exception (which may echo connection strings) is not chained into tracebacks.
    """
    if isinstance(exc, ConnectorError):
        return exc

    text = str(exc).lower()
    line = _first_line(exc)
    detail = redact(line)[:_DETAIL_MAX_CHARS] if line else None
    kwargs = {"connector_type": connector_type, "detail": detail, "driver_error": type(exc).__name__}

    def has(needles: tuple[str, ...]) -> bool:
        return any(n in text for n in needles)

    if has(_AUTHENTICATION):
        return AuthenticationError(f"Authentication to {connector_type} failed; check the credentials", **kwargs)
    if has(_AUTHORIZATION):
        return AuthorizationError(f"Access to {connector_type} was denied; check permissions", **kwargs)
    if isinstance(exc, TimeoutError) or has(_TIMEOUT):
        return ConnectionTimeout(f"Connecting to {connector_type} timed out", **kwargs)
    if has(_UNREACHABLE):
        return ConnectionError(f"Could not reach {connector_type}; check the address and network", **kwargs)
    if has(_NOT_FOUND):
        return ConfigurationError(f"The {connector_type} target was not found; check the configuration", **kwargs)
    return ConnectionError(f"Could not connect to {connector_type}", **kwargs)
