"""ConnectorRegistry (ported from the old registry/factory tests)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import enterprise_connectors
from enterprise_connectors import BaseConnector, ConfigurationError, ConnectorRegistry
from enterprise_connectors.connectors.databases.postgres import PostgresConnector
from tests.fakes import FakeConnector


def test_builtins_include_postgres() -> None:
    registry = ConnectorRegistry()
    assert "postgres" in registry.available_types()
    assert registry.get("postgres") is PostgresConnector
    assert registry.get("  PostGres ") is PostgresConnector


def test_unknown_type_raises_with_available_list() -> None:
    with pytest.raises(ConfigurationError, match="postgres"):
        ConnectorRegistry().get("does-not-exist")


def test_register_custom_and_duplicate() -> None:
    registry = ConnectorRegistry(include_builtins=False)
    registry.register("fake", FakeConnector)
    assert registry.get("FAKE") is FakeConnector
    with pytest.raises(ConfigurationError):
        registry.register("fake", FakeConnector)
    registry.register("fake", FakeConnector, replace=True)
    registry.unregister("fake")
    assert "fake" not in registry


def test_register_lazy_path() -> None:
    registry = ConnectorRegistry(include_builtins=False)
    registry.register("fake", "tests.fakes:FakeConnector")
    assert registry.get("fake") is FakeConnector
    registry.register("broken", "tests.fakes:Nope")
    with pytest.raises(ConfigurationError):
        registry.get("broken")


def test_rejects_non_connectors() -> None:
    class NoName(FakeConnector):
        name = None  # type: ignore[assignment]

    with pytest.raises(ConfigurationError):
        ConnectorRegistry().register("x", dict)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        ConnectorRegistry().register("x", NoName)


def test_every_builtin_is_a_base_connector() -> None:
    registry = ConnectorRegistry()
    for connector_type in registry.available_types():
        assert issubclass(registry.get(connector_type), BaseConnector)


def test_public_api_imports() -> None:
    for name in enterprise_connectors.__all__:
        assert hasattr(enterprise_connectors, name), name


def test_importing_sdk_and_resolving_connectors_does_not_import_drivers() -> None:
    code = (
        "import sys, enterprise_connectors as ec\n"
        "ec.ConnectorRegistry().get('postgres')\n"
        "print('psycopg' in sys.modules)\n"
    )
    src = str(Path(enterprise_connectors.__file__).parents[1])
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": src}
    )
    assert out.stdout.strip() == "False"
