"""PoolManager: process-local pools keyed by connection and security identity.

A PoolKey combines connector type, non-secret config identity, tenant, a keyed
credential fingerprint and the pool config, so different users, tenants,
credentials or incompatible configs never share a pool.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.exceptions import ConnectionPoolError
from ..utils.logging import get_logger
from .connection_pool import ConnectionPool, PoolStats
from .health_check import PoolMaintainer
from .pool_config import PoolConfig

_log = get_logger(__name__)


@dataclass(frozen=True)
class PoolKey:
    connector_type: str
    config_identity: tuple[Any, ...]
    tenant_id: str | None
    credential_fingerprint: str
    pool_config: PoolConfig
    connection_name: str | None = None  # named connections never share a pool with each other

    def __repr__(self) -> str:
        return (
            f"PoolKey(type={self.connector_type!r}, name={self.connection_name!r}, tenant={self.tenant_id!r}, "
            f"credential={self.credential_fingerprint[:8]}…)"
        )


@dataclass
class _Entry:
    pool: ConnectionPool[Any]
    refs: int = 0


class PoolManager:
    def __init__(self) -> None:
        self._pools: dict[PoolKey, _Entry] = {}
        self._lock = threading.Lock()
        self._maintainer: PoolMaintainer | None = None
        self._closed = False

    def acquire(self, key: PoolKey, create: Callable[[], ConnectionPool[Any]]) -> ConnectionPool[Any]:
        """Return the pool for ``key`` (creating it if needed) and take a reference on it."""
        with self._lock:
            if self._closed:
                raise ConnectionPoolError("PoolManager is closed", connector_type=key.connector_type)
            entry = self._pools.get(key)
            if entry is None or entry.pool.closed:
                entry = _Entry(create())
                self._pools[key] = entry
            entry.refs += 1
            return entry.pool

    def release(self, key: PoolKey) -> None:
        """Drop a reference. The pool is closed when its last user releases it."""
        with self._lock:
            entry = self._pools.get(key)
            if entry is None:
                return
            entry.refs -= 1
            if entry.refs > 0:
                return
            del self._pools[key]
        entry.pool.close()

    def prune_all(self) -> None:
        with self._lock:
            pools = [e.pool for e in self._pools.values()]
        for pool in pools:
            try:
                pool.prune()
            except Exception:
                _log.warning("Pool prune failed", exc_info=True)

    def stats(self) -> dict[PoolKey, PoolStats]:
        with self._lock:
            entries = list(self._pools.items())
        return {key: entry.pool.stats() for key, entry in entries}

    def start_maintenance(self, interval: float) -> None:
        """Run prune_all (recycling + health checks + min_size) in a background thread."""
        with self._lock:
            if self._maintainer is None:
                self._maintainer = PoolMaintainer(self.prune_all, interval)
            self._maintainer.start()

    def close_all(self, timeout: float | None = None) -> None:
        """Graceful shutdown: stop maintenance and close every pool."""
        with self._lock:
            self._closed = True
            maintainer, self._maintainer = self._maintainer, None
            entries = list(self._pools.values())
            self._pools.clear()
        if maintainer is not None:
            maintainer.stop()
        for entry in entries:
            entry.pool.close(timeout)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pools)
