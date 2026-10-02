"""ConnectorRegistry: maps connector_type to connector classes.

Built-ins are registered by import path and resolved lazily, so importing the SDK never
imports a connector module (drivers are imported lazily inside connectors as well).
"""

from __future__ import annotations

import importlib
import threading

from ..core.base import BaseConnector
from ..core.exceptions import ConfigurationError

ConnectorClass = type[BaseConnector]  # type: ignore[type-arg]

# connector_type -> "module:Class"
BUILTIN_CONNECTORS: dict[str, str] = {
    "postgres": "enterprise_connectors.connectors.databases.postgres:PostgresConnector",
}


def _normalize(connector_type: str) -> str:
    if not isinstance(connector_type, str) or not connector_type.strip():
        raise ConfigurationError("connector_type must be a non-empty string")
    return connector_type.strip().lower()


class ConnectorRegistry:
    def __init__(self, *, include_builtins: bool = True) -> None:
        self._entries: dict[str, ConnectorClass | str] = dict(BUILTIN_CONNECTORS) if include_builtins else {}
        self._lock = threading.Lock()

    def register(self, connector_type: str, connector: ConnectorClass | str, *, replace: bool = False) -> None:
        """Register a connector class, or a lazy ``"module:Class"`` path."""
        key = _normalize(connector_type)
        if not isinstance(connector, str):
            self._check_class(key, connector)
        with self._lock:
            if key in self._entries and not replace:
                raise ConfigurationError(f"Connector type '{key}' is already registered")
            self._entries[key] = connector

    def unregister(self, connector_type: str) -> None:
        with self._lock:
            self._entries.pop(_normalize(connector_type), None)

    def get(self, connector_type: str) -> ConnectorClass:
        key = _normalize(connector_type)
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            raise ConfigurationError(
                f"Unknown connector type '{key}'. Available: {', '.join(self.available_types()) or 'none'}"
            )
        if isinstance(entry, str):
            cls = self._resolve(key, entry)
            with self._lock:
                self._entries[key] = cls
            return cls
        return entry

    def available_types(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    def __contains__(self, connector_type: object) -> bool:
        return isinstance(connector_type, str) and connector_type.strip().lower() in self._entries

    @staticmethod
    def _resolve(key: str, path: str) -> ConnectorClass:
        module_name, _, attr = path.partition(":")
        try:
            cls = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError):
            raise ConfigurationError(f"Connector '{key}' could not be loaded from '{path}'") from None
        ConnectorRegistry._check_class(key, cls)
        return cls  # type: ignore[no-any-return]

    @staticmethod
    def _check_class(key: str, cls: object) -> None:
        if not (isinstance(cls, type) and issubclass(cls, BaseConnector)):
            raise ConfigurationError(f"Connector '{key}' must be a BaseConnector subclass")
        for attr in ("name", "category"):
            if not isinstance(getattr(cls, attr, None), str):
                raise ConfigurationError(f"Connector '{key}' must define class attribute '{attr}'")
