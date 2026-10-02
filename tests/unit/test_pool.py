"""ConnectionPool checkout/return, exhaustion, recycling, health checks and shutdown."""

from __future__ import annotations

import threading

import pytest

from enterprise_connectors import ConnectionPoolError, PoolConfig, PoolExhausted, PoolTimeout
from enterprise_connectors.pooling.connection_pool import ConnectionPool
from tests.fakes import FakeClock, FakeNative


def make_pool(clock: FakeClock | None = None, **config: object) -> tuple[ConnectionPool[FakeNative], list[FakeNative]]:
    created: list[FakeNative] = []

    def factory() -> FakeNative:
        native = FakeNative("u")
        created.append(native)
        return native

    def validator(native: FakeNative) -> None:
        if not native.healthy:
            raise OSError("dead")

    pool = ConnectionPool(
        factory,
        config=PoolConfig(**config),  # type: ignore[arg-type]
        validator=validator,
        reset=lambda n: not n.dirty,
        closer=FakeNative.close,
        clock=clock or FakeClock(),
    )
    return pool, created


def test_resource_is_reused_after_return() -> None:
    pool, created = make_pool()
    with pool.checkout() as first:
        pass
    with pool.checkout() as second:
        assert second is first
    assert len(created) == 1


def test_concurrent_leases_get_distinct_resources() -> None:
    pool, _ = make_pool(max_size=2)
    a, b = pool.checkout(), pool.checkout()
    assert a.resource is not b.resource
    assert pool.stats().in_use == 2
    a.release()
    b.release()
    assert pool.stats().idle == 2


def test_fail_policy_raises_pool_exhausted() -> None:
    pool, _ = make_pool(max_size=1, on_exhausted="fail")
    lease = pool.checkout()
    with pytest.raises(PoolExhausted):
        pool.checkout()
    lease.release()


def test_wait_policy_times_out() -> None:
    pool, _ = make_pool(max_size=1)
    lease = pool.checkout()
    with pytest.raises(PoolTimeout):
        pool.checkout(timeout=0)
    lease.release()


def test_waiter_gets_resource_when_returned() -> None:
    pool, created = make_pool(max_size=1)
    lease = pool.checkout()
    got: list[FakeNative] = []
    waiter = threading.Thread(target=lambda: got.append(pool.checkout(timeout=5).resource))
    waiter.start()
    lease.release()
    waiter.join(5)
    assert got == [created[0]] and len(created) == 1


def test_max_lifetime_recycles() -> None:
    clock = FakeClock()
    pool, created = make_pool(clock, max_lifetime=60, idle_timeout=None)
    with pool.checkout():
        pass
    clock.advance(61)
    with pool.checkout() as native:
        assert native is created[1]
    assert created[0].closed


def test_idle_timeout_recycles() -> None:
    clock = FakeClock()
    pool, created = make_pool(clock, idle_timeout=10, max_lifetime=None)
    with pool.checkout():
        pass
    clock.advance(11)
    with pool.checkout():
        pass
    assert len(created) == 2 and created[0].closed


def test_failed_health_check_replaces_resource() -> None:
    clock = FakeClock()
    pool, created = make_pool(clock, health_check_interval=5)
    with pool.checkout() as native:
        native.healthy = False
    clock.advance(6)
    with pool.checkout() as native:
        assert native is created[1]
    assert created[0].closed


def test_health_check_skipped_within_interval() -> None:
    pool, created = make_pool(health_check_interval=30)
    with pool.checkout() as native:
        native.healthy = False  # not rechecked yet, so it is reused
    with pool.checkout() as again:
        assert again is created[0]


def test_reset_false_discards_resource() -> None:
    pool, created = make_pool()
    with pool.checkout() as native:
        native.dirty = True
    assert created[0].closed and pool.stats().total == 0


def test_explicit_discard() -> None:
    pool, created = make_pool()
    lease = pool.checkout()
    lease.release(discard=True)
    lease.release()  # idempotent
    assert created[0].closed and pool.stats().total == 0


def test_factory_failure_frees_capacity() -> None:
    calls = []

    def factory() -> FakeNative:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("down")
        return FakeNative("u")

    pool = ConnectionPool(factory, config=PoolConfig(max_size=1))
    with pytest.raises(RuntimeError):
        pool.checkout()
    assert pool.stats().total == 0
    pool.checkout().release()


def test_prune_recycles_and_fills_min_size() -> None:
    clock = FakeClock()
    pool, created = make_pool(clock, min_size=2, max_size=3, idle_timeout=10, max_lifetime=None)
    pool.prune()
    assert pool.stats().idle == 2
    clock.advance(11)
    pool.prune()
    assert created[0].closed and created[1].closed
    assert pool.stats().idle == 2 and len(created) == 4


def test_prune_health_checks_idle_resources() -> None:
    clock = FakeClock()
    pool, created = make_pool(clock, health_check_interval=5)
    with pool.checkout() as native:
        native.healthy = False
    clock.advance(6)
    pool.prune()
    assert created[0].closed and pool.stats().total == 0


def test_close_closes_idle_and_rejects_checkout() -> None:
    pool, created = make_pool()
    with pool.checkout():
        pass
    pool.close(timeout=0)
    assert created[0].closed
    with pytest.raises(ConnectionPoolError):
        pool.checkout()


def test_resource_returned_after_close_is_closed() -> None:
    pool, created = make_pool()
    lease = pool.checkout()
    pool.close(timeout=0)
    lease.release()
    assert created[0].closed and pool.stats().total == 0


def test_close_wakes_waiters() -> None:
    pool, _ = make_pool(max_size=1)
    lease = pool.checkout()
    errors: list[BaseException] = []

    def wait() -> None:
        try:
            pool.checkout(timeout=10)
        except BaseException as exc:
            errors.append(exc)

    waiter = threading.Thread(target=wait)
    waiter.start()
    closer = threading.Thread(target=lambda: pool.close(timeout=5))
    closer.start()
    waiter.join(5)
    lease.release()
    closer.join(5)
    assert errors and isinstance(errors[0], ConnectionPoolError)
