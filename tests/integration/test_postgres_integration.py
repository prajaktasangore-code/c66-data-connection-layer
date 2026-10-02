"""Live PostgreSQL checks. Skipped unless EC_POSTGRES_HOST and the psycopg driver are available.

Set EC_POSTGRES_HOST / _PORT / _DATABASE / _USER / _PASSWORD (see .env.example).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from enterprise_connectors import AuthenticationError, ConnectorManager, EnvCredentialProvider, UsernamePassword

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("EC_POSTGRES_HOST"), reason="EC_POSTGRES_HOST not set"),
]
pytest.importorskip("psycopg")


@pytest.fixture
def config() -> dict[str, object]:
    return {
        "host": os.environ["EC_POSTGRES_HOST"],
        "port": int(os.environ.get("EC_POSTGRES_PORT", "5432")),
        "database": os.environ.get("EC_POSTGRES_DATABASE", "postgres"),
        "connect_timeout": 5,
    }


@pytest.fixture
def manager() -> ConnectorManager:
    mgr = ConnectorManager()
    yield mgr  # type: ignore[misc]
    mgr.close()


def test_connect_and_pool(manager: ConnectorManager, config: dict[str, object]) -> None:
    connector = manager.create("postgres", config, EnvCredentialProvider.username_password("EC_POSTGRES"))
    assert connector.test_connection(), connector.last_result
    with connector.connection() as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_wrong_password_maps_to_authentication_error(manager: ConnectorManager, config: dict[str, object]) -> None:
    user = os.environ.get("EC_POSTGRES_USER", "postgres")
    connector = manager.create(
        "postgres", {**config, "retry": {"max_attempts": 1}}, UsernamePassword(user, "definitely-wrong-password")
    )
    try:
        conn = connector.connect()
    except AuthenticationError:
        return
    conn.close()
    pytest.skip("server accepted a wrong password (pg_hba.conf uses 'trust'); auth failures cannot be tested here")


def test_concurrent_checkouts(manager: ConnectorManager, config: dict[str, object]) -> None:
    connector = manager.create(
        "postgres", config, EnvCredentialProvider.username_password("EC_POSTGRES"), pool_config={"max_size": 3}
    )

    def backend_pid(_: int) -> int:
        with connector.connection() as conn:
            return conn.execute("SELECT pg_backend_pid()").fetchone()[0]  # type: ignore[index,no-any-return]

    with ThreadPoolExecutor(max_workers=6) as pool:
        pids = set(pool.map(backend_pid, range(30)))
    assert 1 <= len(pids) <= 3
