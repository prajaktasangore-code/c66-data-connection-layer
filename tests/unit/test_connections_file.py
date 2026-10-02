"""Named connections from a connections file, and secret references (env://, awssm://)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from enterprise_connectors import (
    AuthenticationError,
    ClientCertificate,
    ConfigurationError,
    ConnectorManager,
    ConnectorRegistry,
    ReferenceCredentialProvider,
    UsernamePassword,
)
from tests.fakes import FakeClock, FakeConnector

ENV = {"DB_HOST": "fake.local", "DB_PORT": "6543", "DB_USER": "alice", "DB_PASSWORD": "s3cr3t-Pa55"}


def good(**overrides: Any) -> dict[str, Any]:
    entry = {
        "type": "fake",
        "config": {"host": "env://DB_HOST", "retry": {"max_attempts": 1}},
        "credentials": {"username": "env://DB_USER", "password": "env://DB_PASSWORD"},
    }
    entry.update(overrides)
    return entry


def load(connections: dict[str, Any], registry: ConnectorRegistry, **kwargs: Any) -> ConnectorManager:
    return ConnectorManager.from_config({"connections": connections}, environ=ENV, registry=registry, **kwargs)


def test_loads_named_connections_and_connects(registry: ConnectorRegistry) -> None:
    manager = load({"db": good()}, registry)
    assert manager.names() == ["db"] and manager.load_errors == {}
    connector = manager.get("db")
    assert manager.get("db") is connector  # same object, so callers share one pool
    with connector.connection() as native:
        assert native.username == "alice"
    manager.close()


def test_invalid_entries_are_skipped_and_reported(registry: ConnectorRegistry) -> None:
    manager = load(
        {
            "ok": good(),
            "no_type": {"config": {}},
            "unknown_type": good(type="nope"),
            "plain_password": good(credentials={"username": "a", "password": "literal"}),
            "vault": good(credentials={"username": "a", "password": "vault://kv/db#pw"}),
            "bad_key": good(colour="blue"),
        },
        registry,
    )
    assert manager.names() == ["ok"]
    assert set(manager.load_errors) == {"no_type", "unknown_type", "plain_password", "vault", "bad_key"}
    assert "'type' is required" in manager.load_errors["no_type"]
    assert "must be a secret reference" in manager.load_errors["plain_password"]
    assert "literal" not in manager.load_errors["plain_password"]
    assert "not supported yet" in manager.load_errors["vault"]
    with pytest.raises(ConfigurationError, match="must be a secret reference"):
        manager.get("plain_password")
    with pytest.raises(ConfigurationError, match="defined: ok"):
        manager.get("never_defined")
    with manager.get("ok").connection():
        pass
    manager.close()


def test_strict_mode_rejects_the_file(registry: ConnectorRegistry) -> None:
    with pytest.raises(ConfigurationError, match="connection 'broken' is invalid"):
        load({"ok": good(), "broken": {"config": {}}}, registry, strict=True)


@pytest.mark.parametrize("data", [{}, {"connections": {}}, {"connections": []}, {"connections": {"a": good()}, "x": 1}])
def test_file_level_problems_raise(data: dict[str, Any], registry: ConnectorRegistry) -> None:
    with pytest.raises(ConfigurationError):
        ConnectorManager.from_config(data, environ=ENV, registry=registry)


def test_config_env_references_are_typed(registry: ConnectorRegistry) -> None:
    manager = ConnectorManager.from_config(
        {
            "connections": {
                "pg": {
                    "type": "postgres",
                    "config": {"host": "env://DB_HOST", "database": "d", "port": "env://DB_PORT"},
                    "credentials": {"username": "env://DB_USER", "password": "env://DB_PASSWORD"},
                }
            }
        },
        environ=ENV,
    )
    assert manager.get("pg").config.port == 6543
    manager.close()


def test_config_rejects_secret_store_refs_and_unset_env(registry: ConnectorRegistry) -> None:
    manager = load(
        {
            "awssm_in_config": good(config={"host": "awssm://x"}),
            "unset": good(config={"host": "env://NOT_SET"}),
        },
        registry,
    )
    assert "only allowed under credentials" in manager.load_errors["awssm_in_config"]
    assert "NOT_SET is not set" in manager.load_errors["unset"]


def test_credentials_are_resolved_at_connect_time_not_load_time(registry: ConnectorRegistry) -> None:
    env = dict(ENV)
    del env["DB_PASSWORD"]
    manager = ConnectorManager.from_config({"connections": {"db": good()}}, environ=env, registry=registry)
    assert manager.names() == ["db"]  # loads fine: nothing secret is read yet
    with pytest.raises(ConfigurationError, match="DB_PASSWORD"):
        manager.get("db").connect()
    env["DB_PASSWORD"] = "now-set"
    assert manager.get("db").connect().username == "alice"  # picked up without reloading
    manager.close()


def test_maintenance_interval_starts_background_maintenance(registry: ConnectorRegistry) -> None:
    manager = ConnectorManager.from_config(
        {"connections": {"db": good()}, "maintenance_interval": 0.05}, environ=ENV, registry=registry
    )
    assert manager.pool_manager._maintainer is not None and manager.pool_manager._maintainer.running
    manager.close()


def test_credentials_type_inference_and_override() -> None:
    registry = ConnectorRegistry()
    pg = {"host": "h", "database": "d", "sslmode": "verify-full"}
    cert = {"username": "u", "cert_path": "c.pem", "key_path": "k.pem"}
    manager = ConnectorManager.from_config(
        {
            "connections": {
                "password_auth": {
                    "type": "postgres",
                    "config": pg,
                    "credentials": {"username": "u", "password": "env://DB_PASSWORD"},
                },
                "cert_auth": {"type": "postgres", "config": pg, "credentials": cert},
                "explicit": {
                    "type": "postgres",
                    "config": pg,
                    "credentials": cert,
                    "credentials_type": "client_certificate",
                },
                "mismatch": {"type": "postgres", "config": pg, "credentials": {"api_key": "env://X"}},
                "bad_kind": {"type": "postgres", "config": pg, "credentials": cert, "credentials_type": "magic"},
            }
        },
        environ=ENV,
        registry=registry,
    )
    assert isinstance(manager.get("password_auth")._provider.get_credentials(), UsernamePassword)
    assert isinstance(manager.get("cert_auth")._provider.get_credentials(), ClientCertificate)
    assert "explicit" in manager.names()
    assert "do not match" in manager.load_errors["mismatch"]
    assert "credentials_type must be one of" in manager.load_errors["bad_kind"]
    manager.close()


def test_from_yaml(tmp_path: Any, registry: ConnectorRegistry) -> None:
    path = tmp_path / "connections.yaml"
    path.write_text(
        "connections:\n  db:\n    type: fake\n    config: {host: env://DB_HOST}\n"
        "    credentials: {username: env://DB_USER, password: env://DB_PASSWORD}\n",
        encoding="utf-8",
    )
    manager = ConnectorManager.from_yaml(path, environ=ENV, registry=registry)
    assert manager.names() == ["db"]
    manager.close()
    with pytest.raises(ConfigurationError, match="Cannot read connections file"):
        ConnectorManager.from_yaml(tmp_path / "missing.yaml")
    (tmp_path / "broken.yaml").write_text("connections: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Cannot read connections file"):
        ConnectorManager.from_yaml(tmp_path / "broken.yaml")


# -- ReferenceCredentialProvider -----------------------------------------------------------------


class _SecretsClient:
    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets
        self.calls = 0

    def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803 - boto3 signature
        self.calls += 1
        if SecretId not in self.secrets:
            raise RuntimeError(f"ResourceNotFoundException {SecretId}")
        return {"SecretString": self.secrets[SecretId]}


def test_awssm_references_with_json_key_region_cache_and_refresh() -> None:
    client = _SecretsClient({"prod/db": json.dumps({"user": "svc", "password": "p1"})})
    regions: list[str | None] = []

    def factory(region: str | None) -> _SecretsClient:
        regions.append(region)
        return client

    clock = FakeClock()
    provider = ReferenceCredentialProvider(
        UsernamePassword,
        {"username": "awssm://prod/db#user?region=eu-west-1", "password": "awssm://prod/db#password?region=eu-west-1"},
        secrets_client_factory=factory,
        cache_ttl=60,
        clock=clock,
    )
    assert provider.get_credentials() == UsernamePassword("svc", "p1")
    assert client.calls == 1 and regions == ["eu-west-1"]  # one fetch serves both fields
    client.secrets["prod/db"] = json.dumps({"user": "svc", "password": "p2"})
    assert provider.get_credentials().password.get_secret_value() == "p1"  # cached
    assert provider.refresh().password.get_secret_value() == "p2"
    clock.advance(61)
    provider.get_credentials()
    assert client.calls == 3


def test_awssm_errors_are_clear_and_secret_free() -> None:
    client = _SecretsClient({"plain": "hunter2-value"})
    missing = ReferenceCredentialProvider(
        UsernamePassword, {"username": "u", "password": "awssm://nope"}, secrets_client_factory=lambda r: client
    )
    with pytest.raises(AuthenticationError, match="could not read secret 'nope'"):
        missing.get_credentials()
    no_key = ReferenceCredentialProvider(
        UsernamePassword,
        {"username": "u", "password": "awssm://plain#password"},
        secrets_client_factory=lambda r: client,
    )
    with pytest.raises(ConfigurationError) as info:
        no_key.get_credentials()
    assert "no JSON key" in str(info.value) and "hunter2" not in str(info.value)
    whole = ReferenceCredentialProvider(
        UsernamePassword, {"username": "u", "password": "awssm://plain"}, secrets_client_factory=lambda r: client
    )
    assert whole.get_credentials().password.get_secret_value() == "hunter2-value"


def test_reference_provider_validation_and_repr() -> None:
    with pytest.raises(ConfigurationError, match="has no field"):
        ReferenceCredentialProvider(UsernamePassword, {"username": "u", "pasword": "env://X"})
    provider = ReferenceCredentialProvider(UsernamePassword, {"username": "u", "password": "literal-secret"})
    assert "literal-secret" not in repr(provider) and "***" in repr(provider)
    assert "env://X" in repr(ReferenceCredentialProvider(UsernamePassword, {"username": "u", "password": "env://X"}))
    assert provider.get_credentials() == UsernamePassword("u", "literal-secret")  # literals allowed unless required


def test_fake_connector_is_used(registry: ConnectorRegistry) -> None:
    assert registry.get("fake") is FakeConnector
