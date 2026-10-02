"""Many simultaneous users: exclusivity, max_size bound, exhaustion and thread safety."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from enterprise_connectors import ConnectorManager, PoolTimeout, UsernamePassword
from tests.fakes import FAST_RETRY, FakeConnector


def test_no_resource_is_shared_and_max_size_holds(manager: ConnectorManager) -> None:
    connector = manager.create(
        "fake", {"retry": FAST_RETRY}, UsernamePassword("u", "p"), pool_config={"max_size": 4, "checkout_timeout": 10}
    )
    assert isinstance(connector, FakeConnector)
    lock = threading.Lock()
    active: set[int] = set()
    peak = 0
    violations: list[str] = []

    def work(user: int) -> None:
        nonlocal peak
        with connector.connection() as native:
            with lock:
                if native.id in active:
                    violations.append(f"resource {native.id} handed to two users")
                active.add(native.id)
                peak = max(peak, len(active))
            time.sleep(0.002)
            with lock:
                active.discard(native.id)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(work, range(200)))

    assert violations == []
    assert peak <= 4 and len(connector.created) <= 4
    (stats,) = manager.pool_manager.stats().values()
    assert stats.in_use == 0 and stats.pending == 0


def test_exhaustion_under_load_times_out_cleanly(manager: ConnectorManager) -> None:
    connector = manager.create(
        "fake",
        {"retry": FAST_RETRY},
        UsernamePassword("u", "p"),
        pool_config={"max_size": 1, "checkout_timeout": 0.05},
    )
    release = threading.Event()
    holding = threading.Event()

    def holder() -> None:
        with connector.connection():
            holding.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    holding.wait(5)
    timeouts = []

    def contender() -> None:
        try:
            with connector.connection():
                pass
        except PoolTimeout:
            timeouts.append(1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: contender(), range(8)))
    release.set()
    t.join(5)
    assert len(timeouts) == 8
    with connector.connection():  # pool recovers once the holder returns
        pass


def test_users_with_different_credentials_never_share(manager: ConnectorManager) -> None:
    users = [UsernamePassword(f"user{i}", f"pw{i}") for i in range(5)]
    connectors = [manager.create("fake", {"retry": FAST_RETRY}, u) for u in users]
    mismatches: list[str] = []

    def work(i: int) -> None:
        c = connectors[i % 5]
        with c.connection() as native:
            if native.username != users[i % 5].username:
                mismatches.append(native.username)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(work, range(100)))
    assert mismatches == []
    assert len(manager.pool_manager) == 5


def test_concurrent_shutdown_is_safe(manager: ConnectorManager) -> None:
    connector = manager.create("fake", {"retry": FAST_RETRY}, UsernamePassword("u", "p"))
    stop = threading.Event()
    errors: list[BaseException] = []

    def work() -> None:
        while not stop.is_set():
            try:
                with connector.connection():
                    pass
            except Exception as exc:  # closed pools raise SDK errors, which are expected here
                from enterprise_connectors import ConnectorError

                if not isinstance(exc, ConnectorError):
                    errors.append(exc)
                return

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    manager.close(timeout=2)
    stop.set()
    for t in threads:
        t.join(5)
    assert errors == []
    assert all(n.closed for n in connector.created)
