"""Reusable validation helpers. Each raises ConfigurationError with a message that never includes secrets."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from ..core.exceptions import ConfigurationError


def require(value: Any, field: str, source: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigurationError(f"{source}: '{field}' is required", connector_type=source)


def ensure_port(value: Any, source: str, field: str = "port") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigurationError(f"{source}: '{field}' must be an integer 1-65535", connector_type=source)


def ensure_positive(value: Any, field: str, source: str, *, allow_zero: bool = False) -> None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not ok or value < 0 or (value == 0 and not allow_zero):
        bound = ">= 0" if allow_zero else "> 0"
        raise ConfigurationError(f"{source}: '{field}' must be a number {bound}", connector_type=source)


def ensure_choice(value: Any, choices: Collection[str], field: str, source: str) -> None:
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ConfigurationError(f"{source}: '{field}' must be one of: {allowed}", connector_type=source)


def ensure_hostname(value: Any, source: str, field: str = "host") -> None:
    require(value, field, source)
    if not isinstance(value, str) or any(c.isspace() for c in value) or "://" in value or len(value) > 253:
        raise ConfigurationError(
            f"{source}: '{field}' must be a bare hostname or IP (no scheme, spaces or credentials)",
            connector_type=source,
        )
    if "@" in value:
        raise ConfigurationError(f"{source}: '{field}' must not embed credentials", connector_type=source)
