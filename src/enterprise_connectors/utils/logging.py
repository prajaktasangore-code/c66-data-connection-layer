"""Logging with secret redaction, plus telemetry/audit hooks.

This module must not import from ``core`` so that ``core.exceptions`` can use it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

REDACTED = "***"

_SENSITIVE = (
    r"(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|authorization|sslpassword)"
)
_SENSITIVE_KEY_RE = re.compile(rf"^[\w.-]*{_SENSITIVE}[\w.-]*$", re.IGNORECASE)

# Order matters: auth schemes first, so "Authorization: Bearer x" loses the token value too.
_AUTH_SCHEME_RE = re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
# scheme://user:password@host
_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s]+@", re.IGNORECASE)
# key=value, key: value, "key": "value" (conninfo strings, query strings, JSON, dict reprs)
_KEY_VALUE_RE = re.compile(
    rf"""(?P<key>(?<![\w-])["']?[\w.-]*{_SENSITIVE}[\w.-]*["']?\s*[:=]\s*)"""
    r"""(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'&,;}\]]+))""",
    re.IGNORECASE,
)


def _mask_key_value(match: re.Match[str]) -> str:
    if match.group("dq") is not None:
        return f'{match.group("key")}"{REDACTED}"'
    if match.group("sq") is not None:
        return f"{match.group('key')}'{REDACTED}'"
    return f"{match.group('key')}{REDACTED}"


def redact(value: object) -> str:
    """Return ``str(value)`` with known secret patterns masked."""
    text = str(value)
    text = _AUTH_SCHEME_RE.sub(rf"\1 {REDACTED}", text)
    text = _URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    return _KEY_VALUE_RE.sub(_mask_key_value, text)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.match(key))


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Copy ``data`` with sensitive keys and secret objects masked (recursively)."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if is_sensitive_key(str(key)) or hasattr(value, "get_secret_value"):
            result[key] = REDACTED
        elif isinstance(value, Mapping):
            result[key] = redact_mapping(value)
        elif isinstance(value, str):
            result[key] = redact(value)
        else:
            result[key] = value
    return result


class RedactingFilter(logging.Filter):
    """Masks secrets in the rendered message, exception text and stack info of a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args: leave the record alone
            return True
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def get_logger(name: str) -> logging.Logger:
    """Return a logger that always redacts secrets before any handler sees the record."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger


_log = get_logger(__name__)


class Events:
    """Telemetry/audit event names."""

    AUTHENTICATED = "auth.authenticated"
    AUTH_REFRESHED = "auth.refreshed"
    CONNECTED = "connection.established"
    CONNECT_FAILED = "connection.failed"
    HEALTH_CHECK = "connection.health_check"
    CHECKOUT = "pool.checkout"
    CHECKIN = "pool.return"
    RECYCLED = "pool.recycled"
    EXHAUSTED = "pool.exhausted"
    POOL_CLOSED = "pool.closed"
    CONNECTOR_CLOSED = "connector.closed"


@dataclass(frozen=True)
class ConnectionEvent:
    """A connection lifecycle event. ``attributes`` are redacted on construction."""

    name: str
    connector_type: str
    success: bool = True
    duration_ms: float | None = None
    error_type: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", redact_mapping(self.attributes))


TelemetryHook = Callable[[ConnectionEvent], None]


class EventEmitter:
    """Fan-out of connection events to registered hooks. A failing hook never breaks a connection."""

    def __init__(self, hooks: Iterable[TelemetryHook] = ()) -> None:
        self._hooks: list[TelemetryHook] = list(hooks)
        self._lock = threading.Lock()

    def add_hook(self, hook: TelemetryHook) -> None:
        with self._lock:
            self._hooks.append(hook)

    def remove_hook(self, hook: TelemetryHook) -> None:
        with self._lock:
            self._hooks.remove(hook)

    def emit(self, event: ConnectionEvent) -> None:
        with self._lock:
            hooks = list(self._hooks)
        for hook in hooks:
            try:
                hook(event)
            except Exception:
                _log.warning("Telemetry hook %r failed for event %s", hook, event.name, exc_info=True)
