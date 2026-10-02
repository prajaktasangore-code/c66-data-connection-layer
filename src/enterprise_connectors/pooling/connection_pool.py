"""ConnectionPool: generic thread-safe checkout/return pool.

Connectors supply ``factory`` (create a native resource), ``validator`` (raise if unhealthy),
``reset`` (return False if the resource must not be reused) and ``closer``.
Each resource is leased to exactly one caller at a time.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Generic, TypeVar

from ..core.exceptions import ConnectionPoolError, PoolExhausted, PoolTimeout
from ..utils.logging import ConnectionEvent, EventEmitter, Events, get_logger
from .pool_config import ExhaustionPolicy, PoolConfig

T = TypeVar("T")

_log = get_logger(__name__)
_LEASE_IDS = itertools.count(1)  # process-wide, so a lease_id is unique in the logs


@dataclass
class _Slot(Generic[T]):
    resource: T
    created_at: float
    last_used_at: float
    last_checked_at: float
    connection_id: str | None = None  # e.g. the Postgres backend PID, fixed for the connection's life


@dataclass(frozen=True)
class PoolStats:
    total: int
    idle: int
    in_use: int
    pending: int
    waiting: int
    max_size: int


class Lease(Generic[T]):
    """Exclusive use of one pooled resource until ``release()``."""

    def __init__(self, pool: ConnectionPool[T], slot: _Slot[T]) -> None:
        self._pool = pool
        self._slot = slot
        self._released = False
        self.id = next(_LEASE_IDS)

    @property
    def resource(self) -> T:
        if self._released:
            raise ConnectionPoolError("Lease has already been released")
        return self._slot.resource

    def release(self, *, discard: bool = False) -> None:
        if not self._released:
            self._released = True
            self._pool._release(self._slot, discard, self.id)

    def __enter__(self) -> T:
        return self.resource

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


class ConnectionPool(Generic[T]):
    def __init__(
        self,
        factory: Callable[[], T],
        *,
        config: PoolConfig | None = None,
        validator: Callable[[T], None] | None = None,
        reset: Callable[[T], bool] | None = None,
        closer: Callable[[T], None] | None = None,
        connector_type: str = "pool",
        events: EventEmitter | None = None,
        identify: Callable[[T], str | None] | None = None,
        attributes: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or PoolConfig()
        self._factory = factory
        self._validator = validator
        self._reset = reset
        self._closer = closer
        self._connector_type = connector_type
        self._events = events
        self._clock = clock
        self._identify = identify
        self._attributes = dict(attributes or {})  # added to every event, e.g. connection_name
        self._cond = threading.Condition(threading.Lock())
        self._idle: deque[_Slot[T]] = deque()
        self._in_use = 0
        self._pending = 0  # being created or health-checked outside the lock; counts toward max_size
        self._waiting = 0
        self._closed = False

    # -- public ------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def checkout(self, timeout: float | None = None) -> Lease[T]:
        """Lease a resource, creating one if below max_size. Raises PoolTimeout / PoolExhausted."""
        wait = self.config.checkout_timeout if timeout is None else timeout
        deadline = self._clock() + wait
        while True:
            stale: list[_Slot[T]] = []
            try:
                slot = self._reserve(deadline, stale)
            finally:
                self._discard_all(stale, reason="expired")
            if slot is None:
                slot = self._create_slot()
                with self._cond:
                    self._pending -= 1
                    if self._closed:
                        self._cond.notify()
                        closed = True
                    else:
                        self._in_use += 1
                        closed = False
                if closed:
                    self._close(slot)
                    raise ConnectionPoolError("Connection pool is closed", connector_type=self._connector_type)
            elif self._needs_check(slot, self._clock()) and not self._validate(slot):
                self._close(slot)
                with self._cond:
                    self._in_use -= 1
                    self._cond.notify()
                self._emit(Events.RECYCLED, slot=slot, reason="health_check_failed")
                continue
            lease = Lease(self, slot)
            self._emit(Events.CHECKOUT, slot=slot, lease_id=lease.id)
            return lease

    def prune(self) -> None:
        """Recycle expired idle resources, health-check idle ones that are due, and top up to min_size."""
        with self._cond:
            if self._closed:
                return
            now = self._clock()
            stale: list[_Slot[T]] = []
            due: list[_Slot[T]] = []
            keep: deque[_Slot[T]] = deque()
            for slot in self._idle:
                if self._is_expired(slot, now):
                    stale.append(slot)
                elif self._needs_check(slot, now):
                    due.append(slot)
                else:
                    keep.append(slot)
            self._idle = keep
            self._pending += len(due)
            self._cond.notify_all()
        self._discard_all(stale, reason="expired")
        for slot in due:
            healthy = self._validate(slot)
            with self._cond:
                self._pending -= 1
                keep_it = healthy and not self._closed
                if keep_it:
                    self._idle.append(slot)
                self._cond.notify()
            if not keep_it:
                self._close(slot)
                self._emit(Events.RECYCLED, slot=slot, reason="health_check_failed")
        self._fill_min_size()

    def stats(self) -> PoolStats:
        with self._cond:
            return PoolStats(
                total=self._total(),
                idle=len(self._idle),
                in_use=self._in_use,
                pending=self._pending,
                waiting=self._waiting,
                max_size=self.config.max_size,
            )

    def close(self, timeout: float | None = None) -> None:
        """Stop new checkouts, close idle resources, and wait for leased ones to be returned."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            self._cond.notify_all()
        self._discard_all(idle, reason="shutdown")
        wait = self.config.shutdown_timeout if timeout is None else timeout
        deadline = self._clock() + wait
        with self._cond:
            while self._in_use + self._pending > 0:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            outstanding = self._in_use
        if outstanding:
            _log.warning(
                "%s pool closed with %d resource(s) still checked out; they will be closed on return",
                self._connector_type,
                outstanding,
            )
        self._emit(Events.POOL_CLOSED, outstanding=outstanding)

    # -- internals ---------------------------------------------------------------------------

    def _total(self) -> int:
        return len(self._idle) + self._in_use + self._pending

    def _is_expired(self, slot: _Slot[T], now: float) -> bool:
        lifetime, idle = self.config.max_lifetime, self.config.idle_timeout
        return (lifetime is not None and now - slot.created_at >= lifetime) or (
            idle is not None and now - slot.last_used_at >= idle
        )

    def _needs_check(self, slot: _Slot[T], now: float) -> bool:
        interval = self.config.health_check_interval
        return self._validator is not None and interval is not None and now - slot.last_checked_at >= interval

    def _reserve(self, deadline: float, stale: list[_Slot[T]]) -> _Slot[T] | None:
        """Under the lock: take an idle slot, or reserve capacity to create one (returns None)."""
        with self._cond:
            while True:
                if self._closed:
                    raise ConnectionPoolError("Connection pool is closed", connector_type=self._connector_type)
                now = self._clock()
                while self._idle:
                    slot = self._idle.pop()  # LIFO keeps a hot working set; cold ones age out
                    if self._is_expired(slot, now):
                        stale.append(slot)
                        continue
                    self._in_use += 1
                    return slot
                if self._total() < self.config.max_size:
                    self._pending += 1
                    return None
                if self.config.on_exhausted is ExhaustionPolicy.FAIL:
                    self._emit(Events.EXHAUSTED, success=False)
                    raise PoolExhausted(
                        f"{self._connector_type} pool exhausted (max_size={self.config.max_size})",
                        connector_type=self._connector_type,
                    )
                remaining = deadline - now
                if remaining <= 0:
                    self._emit(Events.EXHAUSTED, success=False)
                    raise PoolTimeout(
                        f"Timed out waiting for a {self._connector_type} connection (max_size={self.config.max_size})",
                        connector_type=self._connector_type,
                    )
                self._waiting += 1
                try:
                    self._cond.wait(remaining)
                finally:
                    self._waiting -= 1

    def _create_slot(self) -> _Slot[T]:
        """Create outside the lock. The caller has reserved capacity (pending += 1)."""
        try:
            resource = self._factory()
        except BaseException:
            with self._cond:
                self._pending -= 1
                self._cond.notify()
            raise
        now = self._clock()
        connection_id = None
        if self._identify is not None:
            try:
                connection_id = self._identify(resource)
            except Exception:
                _log.debug("Could not identify %s connection", self._connector_type, exc_info=True)
        return _Slot(resource, created_at=now, last_used_at=now, last_checked_at=now, connection_id=connection_id)

    def _release(self, slot: _Slot[T], discard: bool, lease_id: int | None = None) -> None:
        reusable = not discard
        if reusable and self._reset is not None:
            try:
                reusable = bool(self._reset(slot.resource))
            except Exception:
                _log.debug("%s reset failed; discarding resource", self._connector_type, exc_info=True)
                reusable = False
        now = self._clock()
        with self._cond:
            keep = reusable and not self._closed and not self._is_expired(slot, now)
            if keep:
                slot.last_used_at = now
                self._idle.append(slot)
                self._in_use -= 1
                self._cond.notify()
        if not keep:
            # Close before freeing capacity so the source never sees more than max_size connections.
            self._close(slot)
            with self._cond:
                self._in_use -= 1
                self._cond.notify()
            if not self._closed:
                self._emit(Events.RECYCLED, slot=slot, reason="discarded" if not reusable else "expired")
        self._emit(Events.CHECKIN, slot=slot, lease_id=lease_id)

    def _fill_min_size(self) -> None:
        while True:
            with self._cond:
                if self._closed or self._total() >= self.config.min_size:
                    return
                self._pending += 1
            try:
                slot = self._create_slot()
            except Exception:
                _log.warning("Could not pre-create a %s connection", self._connector_type, exc_info=True)
                return
            with self._cond:
                self._pending -= 1
                closed = self._closed
                if not closed:
                    self._idle.append(slot)
                self._cond.notify()
            if closed:
                self._close(slot)
                return

    def _validate(self, slot: _Slot[T]) -> bool:
        if self._validator is None:
            return True
        start = time.perf_counter()
        try:
            self._validator(slot.resource)
        except Exception as exc:
            self._emit(Events.HEALTH_CHECK, success=False, error_type=type(exc).__name__, start=start, slot=slot)
            return False
        slot.last_checked_at = self._clock()
        self._emit(Events.HEALTH_CHECK, start=start, slot=slot)
        return True

    def _close(self, slot: _Slot[T]) -> None:
        if self._closer is None:
            return
        try:
            self._closer(slot.resource)
        except Exception:
            _log.debug("Error closing %s resource", self._connector_type, exc_info=True)

    def _discard_all(self, slots: list[_Slot[T]], *, reason: str) -> None:
        for slot in slots:
            self._close(slot)
            self._emit(Events.RECYCLED, slot=slot, reason=reason)

    def _emit(
        self,
        name: str,
        *,
        success: bool = True,
        error_type: str | None = None,
        start: float | None = None,
        slot: _Slot[T] | None = None,
        lease_id: int | None = None,
        **attributes: object,
    ) -> None:
        if self._events is None:
            return
        attributes = {**self._attributes, **attributes}
        if slot is not None and slot.connection_id is not None:
            attributes["connection_id"] = slot.connection_id
        if lease_id is not None:
            attributes["lease_id"] = lease_id
        duration = (time.perf_counter() - start) * 1000 if start is not None else None
        self._events.emit(
            ConnectionEvent(
                name,
                self._connector_type,
                success=success,
                duration_ms=duration,
                error_type=error_type,
                attributes=attributes,
            )
        )
