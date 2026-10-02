"""Secrets never appear in logs, errors, reprs or events; credentials stay isolated."""

from __future__ import annotations

import logging

import pytest

from enterprise_connectors import (
    ConnectorError,
    ConnectorManager,
    OAuthClientCredentials,
    UsernamePassword,
)
from enterprise_connectors.utils.logging import get_logger
from tests.fakes import FAST_RETRY, FakeConnector, RotatingProvider

SECRET = "Zx9-TopSecret-Value"


def test_logger_redacts_messages_args_and_tracebacks(caplog: pytest.LogCaptureFixture) -> None:
    log = get_logger("enterprise_connectors.test")
    with caplog.at_level(logging.DEBUG, logger="enterprise_connectors"):
        log.info("dsn host=db password=%s", SECRET)
        try:
            raise RuntimeError(f"postgresql://u:{SECRET}@db/x")
        except RuntimeError:
            log.exception("failed")
    rendered = "\n".join(caplog.text.splitlines())
    assert SECRET not in rendered
    assert "***" in rendered


def test_nothing_the_sdk_logs_or_emits_contains_the_secret(
    manager: ConnectorManager, caplog: pytest.LogCaptureFixture
) -> None:
    events = []
    manager.add_hook(events.append)
    connector = manager.create("fake", {"retry": FAST_RETRY}, UsernamePassword("alice", SECRET))
    assert isinstance(connector, FakeConnector)
    connector.failures = [RuntimeError(f"connection refused password={SECRET}")]
    with caplog.at_level(logging.DEBUG, logger="enterprise_connectors"):
        connector.test_connection()
        with connector.connection():
            pass
        connector.close()
    assert SECRET not in caplog.text
    assert all(SECRET not in repr(e) for e in events)
    assert SECRET not in repr(connector) and SECRET not in repr(connector.config)
    assert connector.last_result is not None and SECRET not in repr(connector.last_result)


def test_errors_never_contain_the_secret(manager: ConnectorManager) -> None:
    connector = manager.create("fake", {"retry": FAST_RETRY}, UsernamePassword("alice", SECRET))
    assert isinstance(connector, FakeConnector)
    for message in (f"auth failed: password={SECRET}", f"postgres://alice:{SECRET}@db", f"weird {SECRET!r}"):
        connector.failures = [RuntimeError(message)] * 3
        with pytest.raises(ConnectorError) as info:
            connector.connect()
        assert SECRET not in str(info.value)  # the last case matches no pattern; exact-value scrubbing catches it
        assert info.value.__cause__ is None


def test_pool_keys_do_not_expose_secrets(manager: ConnectorManager) -> None:
    connector = manager.create("fake", {"retry": FAST_RETRY}, UsernamePassword("alice", SECRET))
    with connector.connection():
        pass
    for key in manager.pool_manager.stats():
        assert SECRET not in repr(key) and SECRET not in str(key.config_identity)


def test_tenants_are_isolated_even_with_identical_credentials(manager: ConnectorManager) -> None:
    creds = UsernamePassword("svc", "shared")
    a = manager.create("fake", {"retry": FAST_RETRY, "tenant_id": "tenant-a"}, creds)
    b = manager.create("fake", {"retry": FAST_RETRY, "tenant_id": "tenant-b"}, creds)
    with a.connection() as na, b.connection() as nb:
        assert na is not nb
    with a.connection() as na2, b.connection() as nb2:
        assert na2 is na and nb2 is nb
    assert len(manager.pool_manager) == 2


def test_rotation_does_not_reuse_connections_made_with_old_credentials(manager: ConnectorManager) -> None:
    provider = RotatingProvider(UsernamePassword("alice", "old"), UsernamePassword("alice", "new"))
    connector = manager.create("fake", {"retry": FAST_RETRY}, provider)
    with connector.connection() as old:
        pass
    connector.authenticate(force_refresh=True)
    with connector.connection() as new:
        assert new is not old
    assert old.closed


def test_oauth_credentials_repr_hides_all_secrets() -> None:
    creds = OAuthClientCredentials(client_id="cid", client_secret=SECRET, refresh_token=SECRET + "r")
    assert SECRET not in repr(creds) and "cid" in repr(creds)
