"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from enterprise_connectors import ConnectorManager, ConnectorRegistry, UsernamePassword
from tests.fakes import FAST_RETRY, FakeConnector


@pytest.fixture
def registry() -> ConnectorRegistry:
    reg = ConnectorRegistry()
    reg.register("fake", FakeConnector)
    return reg


@pytest.fixture
def manager(registry: ConnectorRegistry) -> Iterator[ConnectorManager]:
    mgr = ConnectorManager(registry=registry)
    yield mgr
    mgr.close(timeout=0)


@pytest.fixture
def creds() -> UsernamePassword:
    return UsernamePassword(username="alice", password="s3cr3t-Pa55")


@pytest.fixture
def fake_config() -> dict[str, object]:
    return {"host": "fake.local", "retry": FAST_RETRY}
