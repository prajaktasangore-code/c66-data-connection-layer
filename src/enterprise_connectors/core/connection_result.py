"""ConnectionResult: immutable outcome of a connection test or health check. Never wraps the native resource."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .exceptions import ConnectorError


@dataclass(frozen=True)
class ConnectionResult:
    ok: bool
    connector_type: str
    latency_ms: float
    error_type: str | None = None
    message: str | None = None
    checked_at: float = field(default_factory=time.time)
    connection_name: str | None = None  # name from connections.yaml, if any
    connection_id: str | None = None  # server-side id of the tested connection (Postgres: backend PID)

    @classmethod
    def success(cls, connector_type: str, latency_ms: float) -> ConnectionResult:
        return cls(ok=True, connector_type=connector_type, latency_ms=latency_ms)

    @classmethod
    def failure(cls, connector_type: str, latency_ms: float, error: ConnectorError) -> ConnectionResult:
        # str(error) is already redacted by the exception model.
        return cls(
            ok=False,
            connector_type=connector_type,
            latency_ms=latency_ms,
            error_type=type(error).__name__,
            message=str(error),
        )
