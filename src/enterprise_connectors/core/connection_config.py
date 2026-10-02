"""ConnectionConfig model and validation.

Config holds only non-secret settings. Secrets always travel through credentials.
Each connector defines a frozen, keyword-only subclass.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any, TypeVar

from ..utils.logging import is_sensitive_key
from ..utils.retry import RetryPolicy
from ..utils.validation import ensure_positive
from .exceptions import ConfigurationError

ConfigT = TypeVar("ConfigT", bound="ConnectionConfig")

_IDENTITY_EXCLUDE = frozenset({"retry"})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True, kw_only=True)
class ConnectionConfig:
    """Settings shared by every connector."""

    connect_timeout: float = 10.0
    tenant_id: str | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def validate(self) -> None:
        ensure_positive(self.connect_timeout, "connect_timeout", type(self).__name__)
        if not isinstance(self.retry, RetryPolicy):
            raise ConfigurationError(f"{type(self).__name__}: 'retry' must be a RetryPolicy")

    def identity(self) -> tuple[Any, ...]:
        """Hashable, non-secret identity used in pool keys. Differing configs never share a pool."""
        return (type(self).__qualname__,) + tuple(
            (f.name, _freeze(getattr(self, f.name))) for f in fields(self) if f.name not in _IDENTITY_EXCLUDE
        )

    @classmethod
    def from_mapping(cls: type[ConfigT], data: Mapping[str, Any]) -> ConfigT:
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            hint = ""
            if any(is_sensitive_key(k) for k in unknown):
                hint = "; secrets must be passed via credentials=, not config="
            raise ConfigurationError(f"Unknown {cls.__name__} field(s): {', '.join(unknown)}{hint}")
        values = dict(data)
        if isinstance(values.get("retry"), Mapping):
            values["retry"] = RetryPolicy(**values["retry"])
        try:
            return cls(**values)
        except TypeError as exc:
            raise ConfigurationError(f"Invalid {cls.__name__}: {exc}") from None


_CONVERTERS: dict[str, Any] = {
    "int": int,
    "float": float,
    "bool": lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"},
}


def resolve_config_references(
    config_class: type[ConnectionConfig], data: Mapping[str, Any], *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Resolve ``env://NAME`` values in a (non-secret) config mapping, converting to the field's declared type.

    Config is not secret, so it is resolved once at load time. Secret-store references belong under credentials.
    """
    import os

    from .credentials import parse_reference

    env = os.environ if environ is None else environ
    annotations = {f.name: str(f.type) for f in fields(config_class)}
    resolved: dict[str, Any] = {}
    for key, value in data.items():
        ref = parse_reference(value)
        if ref is None:
            resolved[key] = value
            continue
        scheme, name = ref
        if scheme != "env":
            raise ConfigurationError(f"config.{key}: '{scheme}://' references are only allowed under credentials")
        raw = env.get(name)
        if raw in (None, ""):
            raise ConfigurationError(f"config.{key}: environment variable {name} is not set")
        convert = _CONVERTERS.get(annotations.get(key, ""))
        try:
            resolved[key] = convert(raw) if convert else raw
        except ValueError:
            raise ConfigurationError(
                f"config.{key}: environment variable {name} is not a valid {annotations[key]}"
            ) from None
    return resolved
