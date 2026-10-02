"""PostgresConnector against a stub ``psycopg`` module (no server or driver needed)."""

from __future__ import annotations

import enum
import sys
import types
from typing import Any

import pytest

from enterprise_connectors import (
    ClientCertificate,
    ConfigurationError,
    ConnectionError,
    ConnectorManager,
    RetryPolicy,
    UsernamePassword,
)
from enterprise_connectors.connectors.databases.postgres import PostgresConfig, PostgresConnector


class TransactionStatus(enum.IntEnum):
    IDLE = 0
    ACTIVE = 1
    INTRANS = 2
    INERROR = 3
    UNKNOWN = 4


class StubConnection:
    def __init__(self, **params: Any) -> None:
        self.params = params
        self.closed = False
        self.broken = False
        self.autocommit = False
        self.isolation_level: Any = None
        self.read_only: Any = None
        self.deferrable: Any = None
        self.info = types.SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.executed: list[str] = []
        self.rollbacks = 0

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def rollback(self) -> None:
        self.rollbacks += 1
        self.info.transaction_status = TransactionStatus.IDLE

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def psycopg_stub(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    state = types.SimpleNamespace(connections=[], error=None)

    def connect(**params: Any) -> StubConnection:
        if state.error is not None:
            raise state.error
        conn = StubConnection(**params)
        state.connections.append(conn)
        return conn

    module = types.ModuleType("psycopg")
    module.connect = connect  # type: ignore[attr-defined]
    pq = types.ModuleType("psycopg.pq")
    pq.TransactionStatus = TransactionStatus  # type: ignore[attr-defined]
    module.pq = pq  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", module)
    monkeypatch.setitem(sys.modules, "psycopg.pq", pq)
    return state


CONFIG = {"host": "db.internal", "database": "app", "connect_timeout": 2.5, "retry": RetryPolicy(max_attempts=1)}


def make(creds: Any = None, **overrides: Any) -> PostgresConnector:
    connector = ConnectorManager().create("postgres", {**CONFIG, **overrides}, creds or UsernamePassword("app", "pw"))
    assert isinstance(connector, PostgresConnector)
    return connector


def test_connect_passes_libpq_params(psycopg_stub: types.SimpleNamespace) -> None:
    native = make(sslmode="require", options={"target_session_attrs": "read-write"}).connect()
    assert native.params == {
        "host": "db.internal",
        "port": 5432,
        "dbname": "app",
        "connect_timeout": 3,
        "sslmode": "require",
        "application_name": "enterprise-connectors",
        "user": "app",
        "password": "pw",
        "target_session_attrs": "read-write",
    }


def test_client_certificate_auth(psycopg_stub: types.SimpleNamespace) -> None:
    cert = ClientCertificate(username="app", cert_path="c.pem", key_path="k.pem", key_password="kp")
    native = make(cert, sslmode="verify-full").connect()
    assert native.params["sslcert"] == "c.pem" and native.params["sslpassword"] == "kp"
    assert "password" not in native.params
    with pytest.raises(ConfigurationError):
        make(cert, sslmode="prefer").connect()


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": ""},
        {"database": ""},
        {"port": 0},
        {"sslmode": "bogus"},
        {"options": {"password": "x"}},
        {"options": {"sslkey": "k"}},
        {"options": {"my_secret": "x"}},
    ],
)
def test_config_validation(overrides: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        make(**overrides)


def test_ping_uses_select_1_and_ends_transaction(psycopg_stub: types.SimpleNamespace) -> None:
    connector = make()
    assert connector.test_connection()
    conn = psycopg_stub.connections[0]
    assert conn.executed == ["SELECT 1"] and conn.rollbacks == 1 and conn.closed


def test_driver_errors_are_mapped_without_secrets(psycopg_stub: types.SimpleNamespace) -> None:
    psycopg_stub.error = RuntimeError("could not connect: host=db.internal password=Sup3rS3cret! refused")
    with pytest.raises(ConnectionError) as info:
        make(UsernamePassword("app", "Sup3rS3cret!")).connect()
    assert "Sup3rS3cret" not in str(info.value)
    assert info.value.connector_type == "postgres"
    assert info.value.__cause__ is None  # raw driver exception is not chained


def test_reset_rolls_back_and_restores_session_defaults(psycopg_stub: types.SimpleNamespace) -> None:
    connector = make()
    with connector.connection() as conn:
        conn.autocommit = True
        conn.read_only = True
        conn.info.transaction_status = TransactionStatus.INERROR
    assert conn.rollbacks == 1 and conn.autocommit is False and conn.read_only is None
    with connector.connection() as again:
        assert again is conn


@pytest.mark.parametrize("state", ["closed", "broken", "active"])
def test_reset_discards_unusable_connections(psycopg_stub: types.SimpleNamespace, state: str) -> None:
    connector = make()
    with connector.connection() as conn:
        if state == "active":
            conn.info.transaction_status = TransactionStatus.ACTIVE
        else:
            setattr(conn, state, True)
    with connector.connection() as again:
        assert again is not conn


def test_postgres_config_from_mapping() -> None:
    cfg = PostgresConfig.from_mapping({"host": "h", "database": "d", "port": 6432})
    assert cfg.port == 6432
