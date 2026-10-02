"""connection_name, connection_id and lease_id on events and ConnectionResult (for logging)."""

from __future__ import annotations

from typing import Any

from enterprise_connectors import ConnectionEvent, ConnectorManager, ConnectorRegistry, Events, UsernamePassword
from tests.fakes import FAST_RETRY, FakeConnector

ENV = {"U": "alice", "P": "s3cr3t-Pa55"}


def named_manager(registry: ConnectorRegistry, *names: str) -> tuple[ConnectorManager, list[ConnectionEvent]]:
    entry = {
        "type": "fake",
        "config": {"retry": {"max_attempts": 1}},
        "credentials": {"username": "env://U", "password": "env://P"},
    }
    manager = ConnectorManager.from_config({"connections": {n: entry for n in names}}, environ=ENV, registry=registry)
    events: list[ConnectionEvent] = []
    manager.add_hook(events.append)
    return manager, events


def by_name(events: list[ConnectionEvent], name: str) -> list[ConnectionEvent]:
    return [e for e in events if e.name == name]


def test_every_event_carries_the_connection_name(registry: ConnectorRegistry) -> None:
    manager, events = named_manager(registry, "orders_db")
    db = manager.get("orders_db")
    db.test_connection()
    with db.connection():
        pass
    manager.close()
    assert events and all(e.attributes["connection_name"] == "orders_db" for e in events)


def test_connection_id_on_connect_checkout_return_and_health(registry: ConnectorRegistry) -> None:
    manager, events = named_manager(registry, "orders_db")
    db = manager.get("orders_db")
    assert isinstance(db, FakeConnector)
    with db.connection():
        pass
    native_id = str(db.created[0].id)
    for name in (Events.CONNECTED, Events.CHECKOUT, Events.CHECKIN):
        assert by_name(events, name)[0].attributes["connection_id"] == native_id, name
    manager.close()


def test_lease_ids_pair_checkout_with_return_and_are_unique(registry: ConnectorRegistry) -> None:
    manager, events = named_manager(registry, "orders_db")
    db = manager.get("orders_db")
    for _ in range(3):
        with db.connection():
            pass
    outs = [e.attributes["lease_id"] for e in by_name(events, Events.CHECKOUT)]
    ins = [e.attributes["lease_id"] for e in by_name(events, Events.CHECKIN)]
    assert outs == ins and len(set(outs)) == 3
    # same physical connection reused for all three leases
    assert len({e.attributes["connection_id"] for e in by_name(events, Events.CHECKOUT)}) == 1
    manager.close()


def test_test_connection_result_has_name_and_id(registry: ConnectorRegistry) -> None:
    manager, _ = named_manager(registry, "orders_db")
    db = manager.get("orders_db")
    assert db.test_connection()
    result = db.last_result
    assert result is not None and result.connection_name == "orders_db" and result.connection_id is not None
    manager.close()


def test_failed_test_connection_has_name_but_no_id(registry: ConnectorRegistry) -> None:
    manager = ConnectorManager(registry=registry)
    db = manager.create("fake", {"retry": FAST_RETRY}, UsernamePassword("alice", "wrong"), connection_name="bad_db")
    assert db.test_connection() is False
    assert db.last_result is not None
    assert db.last_result.connection_name == "bad_db" and db.last_result.connection_id is None
    manager.close()


def test_differently_named_connections_get_separate_pools(registry: ConnectorRegistry) -> None:
    manager, events = named_manager(registry, "orders_db", "orders_db_replica")  # identical settings
    with manager.get("orders_db").connection() as a, manager.get("orders_db_replica").connection() as b:
        assert a is not b
    assert {k.connection_name for k in manager.pool_manager.stats()} == {"orders_db", "orders_db_replica"}
    manager.close()


def test_unnamed_connectors_still_work(registry: ConnectorRegistry) -> None:
    manager = ConnectorManager(registry=registry)
    seen: list[dict[str, Any]] = []
    manager.add_hook(lambda e: seen.append(dict(e.attributes)))
    with manager.create("fake", {"retry": FAST_RETRY}, UsernamePassword("a", "b")).connection():
        pass
    assert seen and all(a["connection_name"] is None for a in seen)
    manager.close()
