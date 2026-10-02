"""Common contract every connector must pass before it is registered (spec §7, §14).

Runs for every built-in connector plus the test fake. Static checks only; behaviour is
covered by each connector's unit tests and the lifecycle/security suites.
"""

from __future__ import annotations

import inspect
from dataclasses import fields, is_dataclass

import pytest

from enterprise_connectors import BaseConnector, ConnectionConfig, ConnectorRegistry, Credentials
from tests.fakes import FakeConnector

FORBIDDEN = ("fetch", "extract", "paginate", "transform", "load", "embed", "query", "read", "list_sources")
CONTRACT = ("validate", "authenticate", "connect", "test_connection", "connection", "close")
CATEGORIES = {"databases", "warehouses", "cloud", "saas", "apis"}

_registry = ConnectorRegistry()
CONNECTORS = [_registry.get(t) for t in _registry.available_types()] + [FakeConnector]


@pytest.fixture(params=CONNECTORS, ids=lambda c: c.name)
def connector_cls(request: pytest.FixtureRequest) -> type[BaseConnector]:  # type: ignore[type-arg]
    return request.param  # type: ignore[no-any-return]


def test_is_concrete_base_connector(connector_cls: type[BaseConnector]) -> None:  # type: ignore[type-arg]
    assert issubclass(connector_cls, BaseConnector)
    assert not inspect.isabstract(connector_cls)


def test_identity_attributes(connector_cls: type[BaseConnector]) -> None:  # type: ignore[type-arg]
    assert connector_cls.name and connector_cls.name == connector_cls.name.lower()
    assert connector_cls.category in CATEGORIES


def test_public_contract_is_not_overridden(connector_cls: type[BaseConnector]) -> None:  # type: ignore[type-arg]
    # Connectors customise behaviour via the underscore hooks; the public lifecycle stays uniform.
    for method in CONTRACT:
        assert getattr(connector_cls, method) is getattr(BaseConnector, method), method


def test_no_data_access_methods(connector_cls: type[BaseConnector]) -> None:  # type: ignore[type-arg]
    public = [n for n in dir(connector_cls) if not n.startswith("_")]
    offending = [n for n in public if any(n.startswith(f) for f in FORBIDDEN)]
    assert offending == []


def test_config_class_is_frozen_dataclass(connector_cls: type[BaseConnector]) -> None:  # type: ignore[type-arg]
    config_cls = connector_cls.config_class
    assert issubclass(config_cls, ConnectionConfig) and is_dataclass(config_cls)
    assert config_cls.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_config_has_no_secret_fields(connector_cls: type[BaseConnector]) -> None:  # type: ignore[type-arg]
    from enterprise_connectors.utils.logging import is_sensitive_key

    assert [f.name for f in fields(connector_cls.config_class) if is_sensitive_key(f.name)] == []


def test_declares_supported_credentials(connector_cls: type[BaseConnector]) -> None:  # type: ignore[type-arg]
    assert connector_cls.credential_types
    assert all(issubclass(t, Credentials) for t in connector_cls.credential_types)
