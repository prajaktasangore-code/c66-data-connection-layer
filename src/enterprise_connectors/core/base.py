"""BaseConnector abstract contract (connection-focused only).

Public contract (spec §7): validate, authenticate, connect, test_connection, connection, close.
Connector authors implement the underscore hooks: ``_create_native`` and ``_ping``, and
optionally ``_reset_native``, ``_close_native`` and ``_validate_credentials``.

Never add fetch/extract/paginate/transform/load/embed here.
"""

from __future__ import annotations

import importlib
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import ModuleType, TracebackType
from typing import Any, ClassVar, Generic, TypeVar

from ..pooling.connection_pool import ConnectionPool
from ..pooling.health_check import run_check
from ..pooling.pool_config import PoolConfig
from ..pooling.pool_manager import PoolKey, PoolManager
from ..utils.logging import REDACTED, ConnectionEvent, EventEmitter, Events, get_logger
from ..utils.retry import retry_call
from .connection_config import ConnectionConfig
from .connection_result import ConnectionResult
from .credentials import CredentialProvider, Credentials
from .exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    UnsupportedOperation,
    map_driver_error,
)

ConfigT = TypeVar("ConfigT", bound=ConnectionConfig)
NativeT = TypeVar("NativeT")
R = TypeVar("R")

_log = get_logger(__name__)


def import_driver(module: str, *, extra: str) -> ModuleType:
    """Import an optional source driver, or raise ConfigurationError with the install hint."""
    try:
        return importlib.import_module(module)
    except ImportError:
        raise ConfigurationError(
            f"Optional dependency '{module}' is not installed. "
            f'Install it with: pip install "enterprise-connectors[{extra}]"'
        ) from None


_MIN_SCRUB_LENGTH = 4  # shorter values would mangle unrelated text


def _scrub(exc: ConnectorError, credentials: Credentials | None) -> ConnectorError:
    """Remove the exact secret values in use from an error, beyond pattern-based redaction."""
    if credentials is None:
        return exc
    for value in credentials.secret_values():
        if len(value) < _MIN_SCRUB_LENGTH:
            continue
        exc.message = exc.message.replace(value, REDACTED)
        if exc.detail:
            exc.detail = exc.detail.replace(value, REDACTED)
    exc.args = (exc.message,)
    return exc


class BaseConnector(ABC, Generic[ConfigT, NativeT]):
    name: ClassVar[str]
    category: ClassVar[str]
    config_class: ClassVar[type[ConnectionConfig]] = ConnectionConfig
    credential_types: ClassVar[tuple[type[Credentials], ...]] = ()

    def __init__(
        self,
        config: ConfigT,
        credentials: CredentialProvider,
        *,
        pool_manager: PoolManager | None = None,
        pool_config: PoolConfig | None = None,
        events: EventEmitter | None = None,
        connection_name: str | None = None,
    ) -> None:
        self.config = config
        self.connection_name = connection_name  # e.g. "pipeline_db" from connections.yaml; in every event
        self._provider = credentials
        self._owns_pool_manager = pool_manager is None
        # Explicit None checks: an empty PoolManager is falsy (it defines __len__).
        self._pool_manager = PoolManager() if pool_manager is None else pool_manager
        self._pool_config = PoolConfig() if pool_config is None else pool_config
        self._events = EventEmitter() if events is None else events
        self._lock = threading.RLock()
        self._credentials: Credentials | None = None
        self._pool_key: PoolKey | None = None
        self._pool: ConnectionPool[NativeT] | None = None
        self._pool_ref_key: PoolKey | None = None
        self._closed = False
        self.last_result: ConnectionResult | None = None

    # -- connector hooks ---------------------------------------------------------------------

    @abstractmethod
    def _create_native(self, credentials: Credentials) -> NativeT:
        """Open one native connection/client. Driver exceptions are mapped by the caller."""

    @abstractmethod
    def _ping(self, native: NativeT) -> None:
        """Cheap liveness check; raise on failure. Must not read source data."""

    def _reset_native(self, native: NativeT) -> bool:
        """Clean per-caller state before the resource returns to the pool. Return False to discard it."""
        return True

    def _close_native(self, native: NativeT) -> None:
        close = getattr(native, "close", None)
        if callable(close):
            close()

    def _validate_credentials(self, credentials: Credentials) -> None:
        """Connector-specific credential checks beyond the type check."""

    def _native_id(self, native: NativeT) -> object:
        """Server-side id of a native connection for logs (e.g. Postgres backend PID). None if unknown."""
        return None

    # -- public contract ---------------------------------------------------------------------

    def validate(self) -> None:
        if not isinstance(self.config, self.config_class):
            raise ConfigurationError(
                f"{self.name} requires {self.config_class.__name__}, got {type(self.config).__name__}",
                connector_type=self.name,
            )
        self.config.validate()
        if not isinstance(self._provider, CredentialProvider):
            raise ConfigurationError(f"{self.name}: credentials must be a CredentialProvider", connector_type=self.name)

    def authenticate(self, *, force_refresh: bool = False) -> None:
        """Obtain (or re-obtain) credentials from the provider and bind the pool identity to them."""
        with self._lock:
            self._ensure_open()
            try:
                creds = self._provider.refresh() if force_refresh else self._provider.get_credentials()
            except ConnectorError:
                raise
            except Exception as exc:
                raise AuthenticationError(
                    f"Could not obtain credentials for {self.name}",
                    connector_type=self.name,
                    driver_error=type(exc).__name__,
                ) from None
            if self.credential_types and not isinstance(creds, self.credential_types):
                allowed = ", ".join(t.__name__ for t in self.credential_types)
                raise UnsupportedOperation(
                    f"{self.name} does not support {type(creds).__name__} credentials (supported: {allowed})",
                    connector_type=self.name,
                )
            self._validate_credentials(creds)
            new_key = PoolKey(
                connector_type=self.name,
                config_identity=self.config.identity(),
                tenant_id=self.config.tenant_id,
                credential_fingerprint=creds.fingerprint(),
                pool_config=self._pool_config,
                connection_name=self.connection_name,
            )
            self._credentials = creds
            if new_key != self._pool_key:
                self._release_pool()  # rotated credentials get a fresh pool
                self._pool_key = new_key
            self._emit(Events.AUTH_REFRESHED if force_refresh else Events.AUTHENTICATED)

    def connect(self) -> NativeT:
        """Open one raw, unpooled native connection. The caller must close it."""
        return self._with_auth_refresh(lambda: self._establish(self._current_credentials()))

    def test_connection(self) -> bool:
        """Establish, ping and close a connection. Details are in ``last_result``."""

        ids: list[str | None] = []

        def check() -> None:
            native = self.connect()
            ids.append(self._safe_native_id(native))
            try:
                self._call_driver(lambda: self._ping(native))
            finally:
                self._safe_close(native)

        result = replace(
            run_check(check, self.name), connection_name=self.connection_name, connection_id=ids[0] if ids else None
        )
        self.last_result = result
        self._emit(
            Events.HEALTH_CHECK,
            success=result.ok,
            error_type=result.error_type,
            duration_ms=result.latency_ms,
            connection_id=result.connection_id,
        )
        return result.ok

    @contextmanager
    def connection(self) -> Iterator[NativeT]:
        """Check out a pooled native connection for exclusive use; it is returned on exit."""
        lease = self._with_auth_refresh(lambda: self._get_pool().checkout())
        try:
            yield lease.resource
        finally:
            lease.release()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._release_pool()
            self._credentials = None
        if self._owns_pool_manager:
            self._pool_manager.close_all()
        self._emit(Events.CONNECTOR_CLOSED)

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> BaseConnector[ConfigT, NativeT]:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        principal = self._credentials.principal if self._credentials else None
        return (
            f"<{type(self).__name__} type={self.name!r} name={self.connection_name!r} tenant={self.config.tenant_id!r} "
            f"principal={principal!r} closed={self._closed}>"
        )

    # -- internals ---------------------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConnectorError(f"{self.name} connector is closed", connector_type=self.name)

    def _current_credentials(self) -> Credentials:
        with self._lock:
            self._ensure_open()
            if self._credentials is None:
                self.authenticate()
            if self._credentials is None:  # authenticate() either sets credentials or raises
                raise AuthenticationError(f"No credentials for {self.name}", connector_type=self.name)
            return self._credentials

    def _call_driver(self, fn: Callable[[], R], credentials: Credentials | None = None) -> R:
        try:
            return fn()
        except ConnectorError as exc:
            raise _scrub(exc, credentials or self._credentials)  # noqa: B904 - same exception object
        except Exception as exc:
            raise _scrub(map_driver_error(exc, self.name), credentials or self._credentials) from None

    def _establish(self, credentials: Credentials) -> NativeT:
        start = time.perf_counter()

        def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            _log.info("%s connect attempt %d failed (%s); retrying in %.2fs", self.name, attempt, exc, delay)

        try:
            native = retry_call(
                lambda: self._call_driver(lambda: self._create_native(credentials), credentials),
                self.config.retry,
                on_retry=on_retry,
            )
        except ConnectorError as exc:
            self._emit(Events.CONNECT_FAILED, success=False, error_type=type(exc).__name__, start=start)
            raise
        self._emit(Events.CONNECTED, start=start, connection_id=self._safe_native_id(native))
        return native

    def _with_auth_refresh(self, fn: Callable[[], R]) -> R:
        """Run ``fn``; on AuthenticationError re-fetch credentials once and retry if they changed."""
        try:
            return fn()
        except AuthenticationError:
            with self._lock:
                before = self._pool_key
                self.authenticate(force_refresh=True)
                if self._pool_key == before:
                    raise
            return fn()

    def _get_pool(self) -> ConnectionPool[NativeT]:
        with self._lock:
            credentials = self._current_credentials()
            key = self._pool_key
            if key is None:  # set by authenticate(), which _current_credentials() guarantees ran
                raise AuthenticationError(f"{self.name} is not authenticated", connector_type=self.name)
            if self._pool is None or self._pool.closed:
                self._release_pool()

                def create() -> ConnectionPool[Any]:
                    return ConnectionPool(
                        lambda: self._establish(credentials),
                        config=self._pool_config,
                        validator=lambda native: self._call_driver(lambda: self._ping(native)),
                        reset=self._reset_native,
                        closer=self._close_native,
                        connector_type=self.name,
                        events=self._events,
                        identify=self._safe_native_id,
                        attributes=self._event_attributes(),
                    )

                self._pool = self._pool_manager.acquire(key, create)
                self._pool_ref_key = key
            return self._pool

    def _release_pool(self) -> None:
        if self._pool_ref_key is not None:
            self._pool_manager.release(self._pool_ref_key)
        self._pool = None
        self._pool_ref_key = None

    def _safe_native_id(self, native: NativeT) -> str | None:
        try:
            value = self._native_id(native)
        except Exception:
            return None
        return None if value is None else str(value)

    def _event_attributes(self, connection_id: str | None = None) -> dict[str, object]:
        principal = self._credentials.principal if self._credentials else None
        attributes: dict[str, object] = {
            "connection_name": self.connection_name,
            "tenant_id": self.config.tenant_id,
            "principal": principal,
        }
        if connection_id is not None:
            attributes["connection_id"] = connection_id
        return attributes

    def _safe_close(self, native: NativeT) -> None:
        try:
            self._close_native(native)
        except Exception:
            _log.debug("Error closing %s connection", self.name, exc_info=True)

    def _emit(
        self,
        name: str,
        *,
        success: bool = True,
        error_type: str | None = None,
        start: float | None = None,
        duration_ms: float | None = None,
        connection_id: str | None = None,
    ) -> None:
        if duration_ms is None and start is not None:
            duration_ms = (time.perf_counter() - start) * 1000
        self._events.emit(
            ConnectionEvent(
                name,
                self.name,
                success=success,
                duration_ms=duration_ms,
                error_type=error_type,
                attributes=self._event_attributes(connection_id),
            )
        )
