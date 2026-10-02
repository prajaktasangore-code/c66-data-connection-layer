"""ConnectorManager + BaseConnector lifecycle: validate, authenticate, connect, test, pool, close."""

from __future__ import annotations

import pytest

from enterprise_connectors import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    ConnectorError,
    ConnectorManager,
    Events,
    StaticCredentialProvider,
    TokenCredential,
    UnsupportedOperation,
    UsernamePassword,
)
from tests.fakes import FAST_RETRY, FakeConfig, FakeConnector, RotatingProvider


def create(manager: ConnectorManager, creds: object, **config: object) -> FakeConnector:
    connector = manager.create("fake", {"retry": FAST_RETRY, **config}, creds)  # type: ignore[arg-type]
    assert isinstance(connector, FakeConnector)
    return connector


def test_create_accepts_dict_or_config_object(manager: ConnectorManager, creds: UsernamePassword) -> None:
    assert create(manager, creds).config.host == "fake.local"
    connector = manager.create("fake", FakeConfig(host="h"), StaticCredentialProvider(creds))
    assert connector.config.host == "h"


def test_create_validates_eagerly(manager: ConnectorManager, creds: UsernamePassword) -> None:
    with pytest.raises(ConfigurationError):
        create(manager, creds, host="")
    with pytest.raises(ConfigurationError):
        manager.create("unknown", {}, creds)
    with pytest.raises(ConfigurationError):
        manager.create("fake", {}, {"username": "x"})  # type: ignore[arg-type]


def test_unsupported_credential_type(manager: ConnectorManager) -> None:
    connector = create(manager, TokenCredential(token="t"))
    with pytest.raises(UnsupportedOperation):
        connector.authenticate()


def test_connect_returns_native_unpooled(manager: ConnectorManager, creds: UsernamePassword) -> None:
    connector = create(manager, creds)
    native = connector.connect()
    assert native.username == "alice" and not native.closed
    native.close()
    assert len(manager.pool_manager) == 0


def test_test_connection_success_and_failure(manager: ConnectorManager, creds: UsernamePassword) -> None:
    connector = create(manager, creds)
    assert connector.test_connection() is True
    assert connector.last_result is not None and connector.last_result.ok
    assert connector.created[0].closed  # test connection is closed afterwards

    bad = create(manager, UsernamePassword("alice", "wrong"))
    assert bad.test_connection() is False
    assert bad.last_result is not None and bad.last_result.error_type == "AuthenticationError"


def test_connection_context_uses_pool(manager: ConnectorManager, creds: UsernamePassword) -> None:
    connector = create(manager, creds)
    with connector.connection() as first:
        pass
    with connector.connection() as second:
        assert second is first
    assert len(connector.created) == 1 and not first.closed


def test_connection_returned_even_when_block_raises(manager: ConnectorManager, creds: UsernamePassword) -> None:
    connector = create(manager, creds)
    with pytest.raises(ValueError), connector.connection():
        raise ValueError("caller bug")
    (stats,) = manager.pool_manager.stats().values()
    assert stats.in_use == 0 and stats.idle == 1


def test_transient_failures_are_retried(manager: ConnectorManager, creds: UsernamePassword) -> None:
    connector = create(manager, creds)
    connector.failures = [RuntimeError("connection refused"), RuntimeError("timed out")]
    assert connector.connect().username == "alice"


def test_retries_exhausted_raise_mapped_error(manager: ConnectorManager, creds: UsernamePassword) -> None:
    connector = create(manager, creds)
    connector.failures = [RuntimeError("connection refused")] * 3
    with pytest.raises(ConnectionError) as info:
        connector.connect()
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_auth_failure_refreshes_credentials_once(manager: ConnectorManager) -> None:
    provider = RotatingProvider(UsernamePassword("alice", "wrong"), UsernamePassword("alice", "rotated"))
    connector = create(manager, provider)
    with connector.connection() as native:
        assert native.username == "alice"
    assert provider.refreshes == 1


def test_auth_failure_with_unchanged_credentials_raises(manager: ConnectorManager) -> None:
    same = UsernamePassword("alice", "wrong")
    connector = create(manager, RotatingProvider(same, same))
    with pytest.raises(AuthenticationError):
        connector.connect()


def test_rotation_moves_connector_to_new_pool(manager: ConnectorManager) -> None:
    provider = RotatingProvider(UsernamePassword("alice", "v1"), UsernamePassword("alice", "v2"))
    connector = create(manager, provider)
    with connector.connection():
        pass
    old_keys = set(manager.pool_manager.stats())
    connector.authenticate(force_refresh=True)
    with connector.connection():
        pass
    new_keys = set(manager.pool_manager.stats())
    assert len(new_keys) == 1 and new_keys != old_keys
    assert connector.created[0].closed  # old pool was closed with its last reference


def test_connectors_with_same_identity_share_a_pool(manager: ConnectorManager, creds: UsernamePassword) -> None:
    a, b = create(manager, creds), create(manager, creds)
    with a.connection() as first:
        pass
    with b.connection() as second:
        assert second is first
    assert len(manager.pool_manager) == 1
    a.close()
    assert len(manager.pool_manager) == 1  # still referenced by b
    b.close()
    assert len(manager.pool_manager) == 0 and first.closed


def test_closed_connector_rejects_use(manager: ConnectorManager, creds: UsernamePassword) -> None:
    connector = create(manager, creds)
    connector.close()
    connector.close()  # idempotent
    with pytest.raises(ConnectorError):
        connector.connect()


def test_manager_close_shuts_everything_down(registry: object, creds: UsernamePassword) -> None:
    with ConnectorManager(registry=registry) as manager:  # type: ignore[arg-type]
        connector = create(manager, creds)
        with connector.connection() as native:
            pass
    assert native.closed and connector.closed
    with pytest.raises(ConfigurationError):
        create(manager, creds)


def test_standalone_connector_owns_its_pools(creds: UsernamePassword) -> None:
    with FakeConnector(FakeConfig(retry=FAST_RETRY), StaticCredentialProvider(creds)) as connector:
        with connector.connection() as native:
            pass
    assert native.closed


def test_events_are_emitted(manager: ConnectorManager, creds: UsernamePassword) -> None:
    events = []
    manager.add_hook(events.append)
    connector = create(manager, creds, tenant_id="t1")
    with connector.connection():
        pass
    names = [e.name for e in events]
    for expected in (Events.AUTHENTICATED, Events.CONNECTED, Events.CHECKOUT, Events.CHECKIN):
        assert expected in names
    assert all(e.connector_type == "fake" for e in events)
    assert events[0].attributes["tenant_id"] == "t1"


def test_failing_hook_does_not_break_connections(manager: ConnectorManager, creds: UsernamePassword) -> None:
    def broken_hook(event: object) -> None:
        raise RuntimeError("hook bug")

    manager.add_hook(broken_hook)
    assert create(manager, creds).test_connection()


def test_maintenance_thread_runs(manager: ConnectorManager) -> None:
    manager.start_maintenance(interval=0.01)
    assert manager.pool_manager._maintainer is not None and manager.pool_manager._maintainer.running
