"""Credentials and CredentialProvider abstraction (env, static, AWS Secrets Manager, app-supplied).

Secret values are held in ``SecretStr`` and are never rendered by ``repr``/``str``.
Providers are read on every ``get_credentials()`` call, so rotated secrets are picked up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any, ClassVar, TypeVar

from .exceptions import AuthenticationError, ConfigurationError

# Process-local key: fingerprints are stable within a process (pools are process-local)
# but cannot be used offline to confirm a guessed secret.
_FINGERPRINT_KEY = secrets.token_bytes(32)


class SecretStr:
    """A string whose value is only available via ``get_secret_value()``."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("SecretStr value must be a str")
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretStr('**********')"

    def __str__(self) -> str:
        return "**********"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretStr):
            return NotImplemented
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    __hash__ = None  # type: ignore[assignment]


def _as_secret(value: str | SecretStr | None) -> SecretStr | None:
    return SecretStr(value) if isinstance(value, str) else value


@dataclass(frozen=True, repr=False)
class Credentials:
    """Base class for credential sets. Subclasses list secret fields in ``_secret_fields``."""

    _secret_fields: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        for name in self._secret_fields:
            object.__setattr__(self, name, _as_secret(getattr(self, name)))

    @property
    def principal(self) -> str | None:
        """Non-secret identity (e.g. username) safe for logs and audit events."""
        return None

    def secret_values(self) -> tuple[str, ...]:
        """Raw secret values, used only to scrub them from error text."""
        values = (getattr(self, name) for name in self._secret_fields)
        return tuple(v.get_secret_value() for v in values if isinstance(v, SecretStr) and v)

    def fingerprint(self) -> str:
        """Keyed hash of all fields: equal credentials share pools, different ones never do."""
        digest = hmac.new(_FINGERPRINT_KEY, type(self).__name__.encode(), hashlib.sha256)
        for f in fields(self):
            value = getattr(self, f.name)
            raw = value.get_secret_value() if isinstance(value, SecretStr) else repr(value)
            digest.update(f"\x00{f.name}\x00{raw}".encode())
        return digest.hexdigest()[:24]

    def __repr__(self) -> str:
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            shown = "'***'" if f.name in self._secret_fields and value is not None else repr(value)
            parts.append(f"{f.name}={shown}")
        return f"{type(self).__name__}({', '.join(parts)})"

    __str__ = __repr__


@dataclass(frozen=True, repr=False)
class UsernamePassword(Credentials):
    username: str
    password: SecretStr

    _secret_fields: ClassVar[tuple[str, ...]] = ("password",)

    @property
    def principal(self) -> str | None:
        return self.username


@dataclass(frozen=True, repr=False)
class ClientCertificate(Credentials):
    """TLS client-certificate auth. Paths are not secret; the key passphrase is."""

    username: str
    cert_path: str
    key_path: str
    key_password: SecretStr | None = None

    _secret_fields: ClassVar[tuple[str, ...]] = ("key_password",)

    @property
    def principal(self) -> str | None:
        return self.username


@dataclass(frozen=True, repr=False)
class TokenCredential(Credentials):
    token: SecretStr
    expires_at: float | None = None

    _secret_fields: ClassVar[tuple[str, ...]] = ("token",)


@dataclass(frozen=True, repr=False)
class ApiKeyCredential(Credentials):
    api_key: SecretStr

    _secret_fields: ClassVar[tuple[str, ...]] = ("api_key",)


@dataclass(frozen=True, repr=False)
class OAuthClientCredentials(Credentials):
    client_id: str
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = None

    _secret_fields: ClassVar[tuple[str, ...]] = ("client_secret", "refresh_token")

    @property
    def principal(self) -> str | None:
        return self.client_id


C = TypeVar("C", bound=Credentials)


class CredentialProvider(ABC):
    """Supplies credentials to a connector. Implement this to plug in a vault or app store."""

    @abstractmethod
    def get_credentials(self) -> Credentials:
        """Return current credentials. May be called on every connection establishment."""

    def refresh(self) -> Credentials:
        """Return freshly fetched credentials (called after an authentication failure)."""
        return self.get_credentials()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class StaticCredentialProvider(CredentialProvider):
    def __init__(self, credentials: Credentials) -> None:
        if not isinstance(credentials, Credentials):
            raise ConfigurationError("StaticCredentialProvider requires a Credentials instance")
        self._credentials = credentials

    def get_credentials(self) -> Credentials:
        return self._credentials


class EnvCredentialProvider(CredentialProvider):
    """Builds credentials from environment variables, read lazily on every call.

    ``EnvCredentialProvider(UsernamePassword, {"username": "EC_POSTGRES_USER", "password": "EC_POSTGRES_PASSWORD"})``
    """

    def __init__(
        self,
        credential_type: type[Credentials],
        env: Mapping[str, str],
        *,
        optional: tuple[str, ...] = (),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._type = credential_type
        self._env = dict(env)
        self._optional = set(optional)
        self._environ = environ

    @classmethod
    def username_password(cls, prefix: str, **kwargs: Any) -> EnvCredentialProvider:
        """Reads ``{prefix}_USER`` and ``{prefix}_PASSWORD``."""
        return cls(UsernamePassword, {"username": f"{prefix}_USER", "password": f"{prefix}_PASSWORD"}, **kwargs)

    def get_credentials(self) -> Credentials:
        environ = os.environ if self._environ is None else self._environ
        values: dict[str, Any] = {}
        missing = []
        for field_name, var in self._env.items():
            raw = environ.get(var)
            if raw:
                values[field_name] = raw
            elif field_name not in self._optional:
                missing.append(var)
        if missing:
            raise ConfigurationError(f"Missing credential environment variable(s): {', '.join(missing)}")
        return self._type(**values)

    def __repr__(self) -> str:
        return f"EnvCredentialProvider({self._type.__name__}, vars={sorted(self._env.values())})"


class AwsSecretsManagerProvider(CredentialProvider):
    """Reads a JSON secret from AWS Secrets Manager and maps its keys to credential fields.

    ``field_map`` maps credential field -> JSON key (defaults to identical names).
    ``cache_ttl`` seconds avoids a remote call per connection; ``refresh()`` bypasses the cache.
    """

    def __init__(
        self,
        secret_id: str,
        credential_type: type[Credentials],
        *,
        field_map: Mapping[str, str] | None = None,
        region_name: str | None = None,
        cache_ttl: float = 300.0,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._secret_id = secret_id
        self._type = credential_type
        self._field_map = dict(field_map or {f.name: f.name for f in fields(credential_type)})
        self._region = region_name
        self._ttl = cache_ttl
        self._client = client
        self._clock = clock
        self._cached: tuple[float, Credentials] | None = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise ConfigurationError(
                    'AwsSecretsManagerProvider requires boto3: pip install "enterprise-connectors[aws]"'
                ) from None
            self._client = boto3.client("secretsmanager", region_name=self._region)
        return self._client

    def _fetch(self) -> Credentials:
        try:
            response = self._get_client().get_secret_value(SecretId=self._secret_id)
            data = json.loads(response["SecretString"])
        except ConfigurationError:
            raise
        except Exception as exc:
            raise AuthenticationError(
                f"Could not read secret '{self._secret_id}' from AWS Secrets Manager",
                driver_error=type(exc).__name__,
            ) from None
        values = {name: data[key] for name, key in self._field_map.items() if data.get(key) not in (None, "")}
        try:
            return self._type(**values)
        except TypeError:
            raise ConfigurationError(
                f"Secret '{self._secret_id}' is missing fields required by {self._type.__name__}"
            ) from None

    def get_credentials(self) -> Credentials:
        with self._lock:
            now = self._clock()
            if self._cached and now - self._cached[0] < self._ttl:
                return self._cached[1]
            credentials = self._fetch()
            self._cached = (now, credentials)
            return credentials

    def refresh(self) -> Credentials:
        with self._lock:
            self._cached = None
        return self.get_credentials()

    def __repr__(self) -> str:
        return f"AwsSecretsManagerProvider(secret_id={self._secret_id!r}, type={self._type.__name__})"


SUPPORTED_REFERENCE_SCHEMES = ("env", "awssm")
# Recognised secret stores that are not implemented yet; referencing them fails at load time with a clear error.
PLANNED_REFERENCE_SCHEMES = ("vault", "akv")
_REFERENCE_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<body>.+)$", re.IGNORECASE)


def parse_reference(value: object) -> tuple[str, str] | None:
    """Return ``(scheme, body)`` for a secret reference such as ``env://NAME``, else None."""
    if not isinstance(value, str):
        return None
    match = _REFERENCE_RE.match(value.strip())
    if not match:
        return None
    scheme = match.group("scheme").lower()
    if scheme not in SUPPORTED_REFERENCE_SCHEMES + PLANNED_REFERENCE_SCHEMES:
        return None
    return scheme, match.group("body")


class ReferenceCredentialProvider(CredentialProvider):
    """Credentials whose values may be secret references, resolved on every ``get_credentials()``.

    ``env://NAME``                       environment variable NAME
    ``awssm://SECRET_ID#json_key``       a key of a JSON secret in AWS Secrets Manager (``?region=...`` optional)
    ``awssm://SECRET_ID``                the whole SecretString

    Nothing is resolved at construction, so secrets are never held longer than one connection attempt and
    rotated values are picked up. With ``require_secret_refs`` every secret field must be a reference.
    """

    def __init__(
        self,
        credential_type: type[Credentials],
        values: Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
        require_secret_refs: bool = False,
        cache_ttl: float = 300.0,
        secrets_client_factory: Callable[[str | None], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._type = credential_type
        self._values = dict(values)
        self._environ = environ
        self._ttl = cache_ttl
        self._client_factory = secrets_client_factory
        self._clock = clock
        self._clients: dict[str | None, Any] = {}
        self._cache: dict[tuple[str, str | None], tuple[float, str]] = {}
        self._lock = threading.Lock()
        known = {f.name for f in fields(credential_type)}
        unknown = sorted(set(self._values) - known)
        if unknown:
            raise ConfigurationError(
                f"{credential_type.__name__} has no field(s): {', '.join(unknown)} "
                f"(expected: {', '.join(sorted(known))})"
            )
        for name, value in self._values.items():
            ref = parse_reference(value)
            if ref and ref[0] in PLANNED_REFERENCE_SCHEMES:
                raise ConfigurationError(
                    f"credentials field '{name}' uses a '{ref[0]}://' reference, which is not supported yet "
                    f"(supported: {', '.join(s + '://' for s in SUPPORTED_REFERENCE_SCHEMES)})"
                )
            if require_secret_refs and name in credential_type._secret_fields and value not in (None, "") and not ref:
                raise ConfigurationError(
                    f"credentials field '{name}' must be a secret reference such as env://NAME, not a plain value"
                )

    def get_credentials(self) -> Credentials:
        resolved = {name: self._resolve(name, value) for name, value in self._values.items()}
        try:
            return self._type(**resolved)
        except TypeError:
            raise ConfigurationError(f"Missing credential fields for {self._type.__name__}") from None

    def refresh(self) -> Credentials:
        with self._lock:
            self._cache.clear()
        return self.get_credentials()

    def _resolve(self, name: str, value: Any) -> Any:
        ref = parse_reference(value)
        if ref is None:
            return value
        scheme, body = ref
        if scheme == "env":
            environ = os.environ if self._environ is None else self._environ
            raw = environ.get(body)
            if raw in (None, ""):
                raise ConfigurationError(f"environment variable {body} (for credentials field '{name}') is not set")
            return raw
        return self._resolve_awssm(name, body)

    def _resolve_awssm(self, name: str, body: str) -> str:
        target, _, query = body.partition("?")
        secret_id, _, key = target.partition("#")
        region = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("region") or None
        with self._lock:
            cached = self._cache.get((secret_id, region))
            if cached is None or self._clock() - cached[0] >= self._ttl:
                try:
                    secret = self._secrets_client(region).get_secret_value(SecretId=secret_id)["SecretString"]
                except ConfigurationError:
                    raise
                except Exception as exc:
                    raise AuthenticationError(
                        f"could not read secret '{secret_id}' from AWS Secrets Manager "
                        f"(for credentials field '{name}')",
                        driver_error=type(exc).__name__,
                    ) from None
                cached = (self._clock(), secret)
                self._cache[(secret_id, region)] = cached
        if not key:
            return cached[1]
        try:
            value = json.loads(cached[1])[key]
        except (ValueError, KeyError, TypeError):
            raise ConfigurationError(
                f"secret '{secret_id}' has no JSON key '{key}' (for credentials field '{name}')"
            ) from None
        return str(value)

    def _secrets_client(self, region: str | None) -> Any:
        if region not in self._clients:
            if self._client_factory is not None:
                self._clients[region] = self._client_factory(region)
            else:
                try:
                    import boto3
                except ImportError:
                    raise ConfigurationError(
                        'awssm:// references require boto3: pip install "enterprise-connectors[aws]"'
                    ) from None
                self._clients[region] = boto3.client("secretsmanager", region_name=region)
        return self._clients[region]

    def __repr__(self) -> str:
        shown = {
            k: v if parse_reference(v) or k not in self._type._secret_fields else "***" for k, v in self._values.items()
        }
        return f"ReferenceCredentialProvider({self._type.__name__}, {shown})"


def as_provider(credentials: Credentials | CredentialProvider) -> CredentialProvider:
    if isinstance(credentials, CredentialProvider):
        return credentials
    if isinstance(credentials, Credentials):
        return StaticCredentialProvider(credentials)
    raise ConfigurationError(
        f"credentials must be a Credentials instance or a CredentialProvider (got {type(credentials).__name__})"
    )
