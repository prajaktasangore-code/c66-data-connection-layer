"""ConnectorManager: public entry point to create and manage connectors.

Connections can also be declared in a YAML file and looked up by name::

    manager = ConnectorManager.from_yaml("connections.yaml")
    with manager.get("analytics_db").connection() as conn:
        ...
"""

from __future__ import annotations

import re
import threading
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import MISSING, fields
from pathlib import Path
from types import TracebackType
from typing import Any

from ..core.base import BaseConnector
from ..core.connection_config import ConnectionConfig, resolve_config_references
from ..core.credentials import (
    ApiKeyCredential,
    ClientCertificate,
    CredentialProvider,
    Credentials,
    OAuthClientCredentials,
    ReferenceCredentialProvider,
    TokenCredential,
    UsernamePassword,
    as_provider,
)
from ..core.exceptions import ConfigurationError
from ..pooling.pool_config import PoolConfig
from ..pooling.pool_manager import PoolManager
from ..registry.connector_registry import ConnectorRegistry
from ..utils.logging import EventEmitter, TelemetryHook, get_logger, redact

_log = get_logger(__name__)

CREDENTIAL_TYPES: dict[str, type[Credentials]] = {
    "username_password": UsernamePassword,
    "client_certificate": ClientCertificate,
    "token": TokenCredential,
    "api_key": ApiKeyCredential,
    "oauth_client": OAuthClientCredentials,
}
_ENTRY_KEYS = {"type", "config", "credentials", "credentials_type", "pool"}
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _pick_credential_type(
    connector_cls: type[BaseConnector[Any, Any]], values: Mapping[str, Any], declared: str | None
) -> type[Credentials]:
    """Use ``credentials_type`` if given, else the connector's one supported type whose fields fit the keys."""
    if declared is not None:
        if declared not in CREDENTIAL_TYPES:
            raise ConfigurationError(f"credentials_type must be one of: {', '.join(CREDENTIAL_TYPES)}")
        return CREDENTIAL_TYPES[declared]
    keys = set(values)
    matches = []
    for cls in connector_cls.credential_types:
        all_fields = {f.name for f in fields(cls)}
        required = {f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING}
        if keys <= all_fields and required <= keys:
            matches.append(cls)
    if len(matches) == 1:
        return matches[0]
    accepted = "; ".join(
        f"{c.__name__}({', '.join(f.name for f in fields(c))})" for c in connector_cls.credential_types
    )
    if not matches:
        raise ConfigurationError(f"credentials {sorted(keys)} do not match a supported type: {accepted}")
    raise ConfigurationError(f"credentials are ambiguous; set credentials_type ({', '.join(CREDENTIAL_TYPES)})")


class ConnectorManager:
    """Creates connectors and owns the process-local pools they share.

    manager = ConnectorManager()
    connector = manager.create(connector_type="postgres", config=config, credentials=credentials)
    """

    def __init__(
        self,
        *,
        registry: ConnectorRegistry | None = None,
        pool_manager: PoolManager | None = None,
        hooks: Iterable[TelemetryHook] = (),
    ) -> None:
        self.registry = ConnectorRegistry() if registry is None else registry
        self.pool_manager = PoolManager() if pool_manager is None else pool_manager  # empty one is falsy
        self.events = EventEmitter(hooks)
        self._connectors: weakref.WeakSet[BaseConnector[Any, Any]] = weakref.WeakSet()
        self._named: dict[str, BaseConnector[Any, Any]] = {}
        self._load_errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._closed = False

    def create(
        self,
        connector_type: str,
        config: ConnectionConfig | Mapping[str, Any],
        credentials: Credentials | CredentialProvider,
        *,
        pool_config: PoolConfig | Mapping[str, Any] | None = None,
        connection_name: str | None = None,
    ) -> BaseConnector[Any, Any]:
        """Create and validate a connector. Credentials are fetched lazily on first use.

        ``connection_name`` labels every event and test result (``from_yaml`` sets it to the entry name).
        """
        if self._closed:
            raise ConfigurationError("ConnectorManager is closed")
        cls = self.registry.get(connector_type)
        if isinstance(config, Mapping):
            config = cls.config_class.from_mapping(config)
        if isinstance(pool_config, Mapping):
            try:
                pool_config = PoolConfig(**pool_config)
            except TypeError as exc:
                raise ConfigurationError(f"Invalid pool_config: {exc}") from None
        connector = cls(
            config,
            as_provider(credentials),
            pool_manager=self.pool_manager,
            pool_config=pool_config,
            events=self.events,
            connection_name=connection_name,
        )
        connector.validate()
        with self._lock:
            self._connectors.add(connector)
        return connector

    # -- named connections (connections file) -------------------------------------------------

    @classmethod
    def from_yaml(
        cls, path: str | Path, *, environ: Mapping[str, str] | None = None, **kwargs: Any
    ) -> ConnectorManager:
        """Load named connections from a YAML file (needs the ``yaml`` extra). See ``from_config``; ``strict`` is
        accepted there via ``kwargs``."""
        try:
            import yaml
        except ImportError:
            raise ConfigurationError(
                'Loading YAML requires PyYAML: pip install "enterprise-connectors[yaml]"'
            ) from None
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"Cannot read connections file {path}: {redact(exc)}") from None
        return cls.from_config(data or {}, environ=environ, **kwargs)

    @classmethod
    def from_config(
        cls,
        data: Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
        strict: bool = False,
        **kwargs: Any,
    ) -> ConnectorManager:
        """Create a manager from ``{"connections": {name: entry}, "maintenance_interval": seconds}``.

        Each entry is validated independently. An invalid entry is skipped and its reason recorded in
        ``load_errors`` (``get(name)`` raises with that reason); the valid entries are usable. With
        ``strict=True`` the first invalid entry raises instead and nothing is kept.
        No connection is opened and no secret is read until first use.
        """
        if not isinstance(data, Mapping) or not isinstance(data.get("connections"), Mapping) or not data["connections"]:
            raise ConfigurationError("connections file must contain a non-empty 'connections' mapping")
        unknown = sorted(set(data) - {"connections", "maintenance_interval"})
        if unknown:
            raise ConfigurationError(f"Unknown top-level key(s) in connections file: {', '.join(unknown)}")
        manager = cls(**kwargs)
        try:
            for name, entry in data["connections"].items():
                try:
                    manager.add(str(name), entry, environ=environ)
                except ConfigurationError as exc:
                    if strict:
                        raise
                    manager._load_errors[str(name)] = str(exc)
                    _log.warning("Connection '%s' was not loaded: %s", name, exc)
            if data.get("maintenance_interval"):
                manager.start_maintenance(float(data["maintenance_interval"]))
        except BaseException:
            manager.close(timeout=0)
            raise
        return manager

    @property
    def load_errors(self) -> dict[str, str]:
        """Connections from the file that were not loaded, with the reason (secrets never included)."""
        return dict(self._load_errors)

    def add(
        self, name: str, entry: Mapping[str, Any], *, environ: Mapping[str, str] | None = None
    ) -> BaseConnector[Any, Any]:
        """Register one named connection from a mapping shaped like one connections-file entry."""
        if not _NAME_RE.match(name):
            raise ConfigurationError(f"Invalid connection name {name!r} (use letters, digits, '_', '-', '.')")
        if name in self._named:
            raise ConfigurationError(f"Connection '{name}' is already defined")
        try:
            if not isinstance(entry, Mapping):
                raise ConfigurationError("entry must be a mapping")
            unknown = sorted(set(entry) - _ENTRY_KEYS)
            if unknown:
                raise ConfigurationError(f"unknown key(s): {', '.join(unknown)}")
            if not entry.get("type"):
                raise ConfigurationError("'type' is required")
            connector_cls = self.registry.get(str(entry["type"]))
            config = resolve_config_references(connector_cls.config_class, entry.get("config") or {}, environ=environ)
            values = entry.get("credentials") or {}
            credential_type = _pick_credential_type(connector_cls, values, entry.get("credentials_type"))
            provider = ReferenceCredentialProvider(credential_type, values, environ=environ, require_secret_refs=True)
            connector = self.create(
                str(entry["type"]), config, provider, pool_config=entry.get("pool"), connection_name=name
            )
        except ConfigurationError as exc:
            raise ConfigurationError(
                f"connection '{name}' is invalid and was not loaded - {exc}", connector_type=exc.connector_type
            ) from None
        self._named[name] = connector
        return connector

    def get(self, name: str) -> BaseConnector[Any, Any]:
        """The connector registered under ``name``: always the same object, so callers share its pool."""
        if name in self._load_errors:
            raise ConfigurationError(self._load_errors[name])
        try:
            return self._named[name]
        except KeyError:
            defined = ", ".join(sorted(self._named)) or "none"
            raise ConfigurationError(f"No connection named '{name}' (defined: {defined})") from None

    def names(self) -> list[str]:
        return sorted(self._named)

    def available_types(self) -> list[str]:
        return self.registry.available_types()

    def add_hook(self, hook: TelemetryHook) -> None:
        self.events.add_hook(hook)

    def start_maintenance(self, interval: float = 30.0) -> None:
        """Background recycling of stale resources, idle health checks and min_size top-up."""
        self.pool_manager.start_maintenance(interval)

    def close(self, timeout: float | None = None) -> None:
        """Graceful shutdown of every connector and pool created by this manager."""
        with self._lock:
            self._closed = True
            connectors = list(self._connectors)
        for connector in connectors:
            connector.close()
        self.pool_manager.close_all(timeout)

    def __enter__(self) -> ConnectorManager:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
