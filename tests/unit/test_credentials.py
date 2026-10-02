"""Credentials, SecretStr and CredentialProviders (env provider ported from the lazy-settings test)."""

from __future__ import annotations

import json

import pytest

from enterprise_connectors import (
    AwsSecretsManagerProvider,
    ConfigurationError,
    EnvCredentialProvider,
    SecretStr,
    StaticCredentialProvider,
    UsernamePassword,
)
from enterprise_connectors.core.credentials import AuthenticationError, as_provider
from tests.fakes import FakeClock


def test_secret_str_never_renders_value() -> None:
    secret = SecretStr("hunter2")
    assert "hunter2" not in repr(secret)
    assert "hunter2" not in str(secret)
    assert "hunter2" not in f"{secret}"
    assert secret.get_secret_value() == "hunter2"
    assert secret == SecretStr("hunter2") and secret != SecretStr("other")


def test_credentials_coerce_and_redact() -> None:
    creds = UsernamePassword(username="alice", password="hunter2")
    assert isinstance(creds.password, SecretStr)
    assert "hunter2" not in repr(creds) and "alice" in repr(creds)
    assert creds.principal == "alice"


def test_fingerprint_distinguishes_credentials_without_revealing_them() -> None:
    a = UsernamePassword("alice", "p1")
    assert a.fingerprint() == UsernamePassword("alice", "p1").fingerprint()
    assert a.fingerprint() != UsernamePassword("alice", "p2").fingerprint()
    assert a.fingerprint() != UsernamePassword("bob", "p1").fingerprint()
    assert "p1" not in a.fingerprint()


def test_env_provider_reads_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = EnvCredentialProvider.username_password("EC_TEST_PG")
    monkeypatch.setenv("EC_TEST_PG_USER", "alice")
    monkeypatch.setenv("EC_TEST_PG_PASSWORD", "one")
    assert provider.get_credentials() == UsernamePassword("alice", "one")
    monkeypatch.setenv("EC_TEST_PG_PASSWORD", "two")  # rotation is picked up
    assert provider.get_credentials() == UsernamePassword("alice", "two")


def test_env_provider_missing_vars_names_vars_only() -> None:
    provider = EnvCredentialProvider.username_password("EC_X", environ={"EC_X_USER": "alice"})
    with pytest.raises(ConfigurationError, match="EC_X_PASSWORD"):
        provider.get_credentials()


def test_as_provider() -> None:
    creds = UsernamePassword("a", "b")
    assert isinstance(as_provider(creds), StaticCredentialProvider)
    provider = StaticCredentialProvider(creds)
    assert as_provider(provider) is provider
    with pytest.raises(ConfigurationError):
        as_provider({"username": "a"})  # type: ignore[arg-type]


class _FakeSecretsClient:
    def __init__(self, secret: dict[str, str]) -> None:
        self.secret = secret
        self.calls = 0

    def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803 - boto3 signature
        self.calls += 1
        if SecretId == "missing":
            raise RuntimeError("ResourceNotFoundException: secret hunter2 not found")
        return {"SecretString": json.dumps(self.secret)}


def test_secrets_manager_provider_caches_and_refreshes() -> None:
    client = _FakeSecretsClient({"username": "alice", "password": "v1"})
    clock = FakeClock()
    provider = AwsSecretsManagerProvider("db/pg", UsernamePassword, client=client, cache_ttl=60, clock=clock)
    assert provider.get_credentials() == UsernamePassword("alice", "v1")
    provider.get_credentials()
    assert client.calls == 1
    client.secret["password"] = "v2"
    assert provider.refresh() == UsernamePassword("alice", "v2")
    clock.advance(61)
    provider.get_credentials()
    assert client.calls == 3


def test_secrets_manager_errors_do_not_leak() -> None:
    provider = AwsSecretsManagerProvider("missing", UsernamePassword, client=_FakeSecretsClient({}))
    with pytest.raises(AuthenticationError) as info:
        provider.get_credentials()
    assert "hunter2" not in str(info.value)


def test_secrets_manager_field_map() -> None:
    client = _FakeSecretsClient({"user": "alice", "pass": "x"})
    provider = AwsSecretsManagerProvider(
        "s", UsernamePassword, client=client, field_map={"username": "user", "password": "pass"}
    )
    assert provider.get_credentials() == UsernamePassword("alice", "x")
