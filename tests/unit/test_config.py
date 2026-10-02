"""ConnectionConfig, PoolConfig, RetryPolicy and validation helpers."""

from __future__ import annotations

import pytest

from enterprise_connectors import ConfigurationError, PoolConfig, RetryPolicy
from enterprise_connectors.core import exceptions as errors
from enterprise_connectors.pooling.pool_config import ExhaustionPolicy
from enterprise_connectors.utils import validation
from enterprise_connectors.utils.retry import retry_call
from tests.fakes import FakeConfig


def test_from_mapping_builds_config_and_nested_retry() -> None:
    cfg = FakeConfig.from_mapping({"host": "h", "retry": {"max_attempts": 5}})
    assert cfg.host == "h" and cfg.retry.max_attempts == 5


def test_from_mapping_rejects_unknown_and_secret_fields() -> None:
    with pytest.raises(ConfigurationError, match="nope"):
        FakeConfig.from_mapping({"nope": 1})
    with pytest.raises(ConfigurationError, match="credentials="):
        FakeConfig.from_mapping({"password": "x"})


def test_identity_is_hashable_and_ignores_retry() -> None:
    a = FakeConfig(host="h", retry=RetryPolicy(max_attempts=1))
    b = FakeConfig(host="h", retry=RetryPolicy(max_attempts=9))
    assert hash(a.identity()) == hash(b.identity())
    assert a.identity() != FakeConfig(host="other").identity()
    assert a.identity() != FakeConfig(host="h", tenant_id="t1").identity()


def test_config_validation() -> None:
    with pytest.raises(ConfigurationError):
        FakeConfig(host="").validate()
    with pytest.raises(ConfigurationError):
        FakeConfig(connect_timeout=0).validate()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_size": 0},
        {"min_size": 6, "max_size": 5},
        {"checkout_timeout": -1},
        {"max_lifetime": 0},
        {"health_check_interval": -1},
        {"on_exhausted": "explode"},
    ],
)
def test_pool_config_rejects_invalid(kwargs: dict[str, object]) -> None:
    with pytest.raises((ConfigurationError, ValueError)):
        PoolConfig(**kwargs)  # type: ignore[arg-type]


def test_pool_config_coerces_policy() -> None:
    assert PoolConfig(on_exhausted="fail").on_exhausted is ExhaustionPolicy.FAIL  # type: ignore[arg-type]


def test_retry_policy_validation_and_backoff() -> None:
    with pytest.raises(ConfigurationError):
        RetryPolicy(max_attempts=0)
    policy = RetryPolicy(initial_backoff=1, multiplier=2, max_backoff=3, jitter=0)
    assert [policy.backoff(n) for n in (1, 2, 3, 4)] == [1, 2, 3, 3]


def test_retry_call_retries_only_transient_errors() -> None:
    calls = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise errors.ConnectionError("down")
        return "ok"

    assert retry_call(flaky, RetryPolicy(max_attempts=3, initial_backoff=0), sleep=lambda _: None) == "ok"

    def denied() -> None:
        calls.append(1)
        raise errors.AuthenticationError("no")

    calls.clear()
    with pytest.raises(errors.AuthenticationError):
        retry_call(denied, RetryPolicy(max_attempts=5, initial_backoff=0), sleep=lambda _: None)
    assert len(calls) == 1


def test_retry_call_gives_up_after_max_attempts() -> None:
    delays: list[float] = []
    with pytest.raises(errors.ConnectionTimeout):
        retry_call(
            lambda: (_ for _ in ()).throw(errors.ConnectionTimeout("slow")),
            RetryPolicy(max_attempts=3, initial_backoff=0.1, jitter=0),
            sleep=delays.append,
        )
    assert delays == [0.1, 0.2]


@pytest.mark.parametrize("host", ["db.example.com", "10.0.0.1", "::1"])
def test_ensure_hostname_accepts(host: str) -> None:
    validation.ensure_hostname(host, "pg")


@pytest.mark.parametrize("host", ["", "postgres://db", "user:pw@db", "db host", None])
def test_ensure_hostname_rejects(host: object) -> None:
    with pytest.raises(ConfigurationError):
        validation.ensure_hostname(host, "pg")


@pytest.mark.parametrize("port", [0, 70000, "5432", True])
def test_ensure_port_rejects(port: object) -> None:
    with pytest.raises(ConfigurationError):
        validation.ensure_port(port, "pg")
