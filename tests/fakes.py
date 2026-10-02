"""In-memory connector used to test the SDK lifecycle without any real source."""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass

from enterprise_connectors.core.base import BaseConnector
from enterprise_connectors.core.connection_config import ConnectionConfig
from enterprise_connectors.core.credentials import CredentialProvider, Credentials, UsernamePassword
from enterprise_connectors.utils.retry import RetryPolicy
from enterprise_connectors.utils.validation import require

FAST_RETRY = RetryPolicy(max_attempts=3, initial_backoff=0, jitter=0)


@dataclass(frozen=True, kw_only=True)
class FakeConfig(ConnectionConfig):
    host: str = "fake.local"

    def validate(self) -> None:
        super().validate()
        require(self.host, "host", "fake")


class FakeNative:
    _ids = itertools.count(1)

    def __init__(self, username: str) -> None:
        self.id = next(self._ids)
        self.username = username
        self.closed = False
        self.healthy = True
        self.dirty = False
        self.in_use_by: int | None = None

    def close(self) -> None:
        self.closed = True


class FakeConnector(BaseConnector[FakeConfig, FakeNative]):
    name = "fake"
    category = "databases"
    config_class = FakeConfig
    credential_types = (UsernamePassword,)

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.created: list[FakeNative] = []
        self.failures: list[Exception] = []  # raised (in order) by the next _create_native calls
        self._created_lock = threading.Lock()

    def _create_native(self, credentials: Credentials) -> FakeNative:
        assert isinstance(credentials, UsernamePassword)
        if self.failures:
            raise self.failures.pop(0)
        if credentials.password.get_secret_value() == "wrong":
            raise RuntimeError(f'FATAL: password authentication failed for user "{credentials.username}"')
        native = FakeNative(credentials.username)
        with self._created_lock:
            self.created.append(native)
        return native

    def _native_id(self, native: FakeNative) -> object:
        return native.id

    def _ping(self, native: FakeNative) -> None:
        if not native.healthy:
            raise OSError("server closed the connection unexpectedly")

    def _reset_native(self, native: FakeNative) -> bool:
        return not native.closed and not native.dirty


class RotatingProvider(CredentialProvider):
    """Returns ``stale`` until refresh() is called, then ``fresh``."""

    def __init__(self, stale: Credentials, fresh: Credentials) -> None:
        self.current = stale
        self.fresh = fresh
        self.refreshes = 0

    def get_credentials(self) -> Credentials:
        return self.current

    def refresh(self) -> Credentials:
        self.refreshes += 1
        self.current = self.fresh
        return self.current


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
