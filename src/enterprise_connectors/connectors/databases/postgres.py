"""PostgreSQL reference connector. Returns native ``psycopg.Connection`` objects (psycopg 3).

Install with ``pip install "enterprise-connectors[postgres]"``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...core.base import BaseConnector, import_driver
from ...core.connection_config import ConnectionConfig
from ...core.credentials import ClientCertificate, Credentials, UsernamePassword
from ...core.exceptions import ConfigurationError
from ...utils.logging import is_sensitive_key
from ...utils.validation import ensure_choice, ensure_hostname, ensure_port, require

if TYPE_CHECKING:
    import psycopg

SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
CERT_SSL_MODES = ("require", "verify-ca", "verify-full")

# libpq parameters controlled by config fields or credentials; not allowed in ``options``.
_RESERVED_OPTIONS = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "dbname",
        "user",
        "password",
        "passfile",
        "connect_timeout",
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "sslpassword",
        "application_name",
        "service",
    }
)


@dataclass(frozen=True, kw_only=True)
class PostgresConfig(ConnectionConfig):
    host: str
    database: str
    port: int = 5432
    sslmode: str = "prefer"
    sslrootcert: str | None = None
    application_name: str = "enterprise-connectors"
    options: Mapping[str, str] = field(default_factory=dict)  # extra non-secret libpq parameters

    def validate(self) -> None:
        super().validate()
        ensure_hostname(self.host, "postgres")
        require(self.database, "database", "postgres")
        ensure_port(self.port, "postgres")
        ensure_choice(self.sslmode, SSL_MODES, "sslmode", "postgres")
        reserved = sorted(set(self.options) & _RESERVED_OPTIONS)
        if reserved:
            raise ConfigurationError(
                f"postgres: options may not set {', '.join(reserved)}; use config fields or credentials",
                connector_type="postgres",
            )
        if any(is_sensitive_key(k) for k in self.options):
            raise ConfigurationError(
                "postgres: secrets must be passed via credentials, not options", connector_type="postgres"
            )


class PostgresConnector(BaseConnector[PostgresConfig, "psycopg.Connection[Any]"]):
    name = "postgres"
    category = "databases"
    config_class = PostgresConfig
    credential_types = (UsernamePassword, ClientCertificate)

    def _validate_credentials(self, credentials: Credentials) -> None:
        if isinstance(credentials, ClientCertificate) and self.config.sslmode not in CERT_SSL_MODES:
            raise ConfigurationError(
                f"postgres: client-certificate auth requires sslmode in {', '.join(CERT_SSL_MODES)}",
                connector_type=self.name,
            )

    def _connect_params(self, credentials: Credentials) -> dict[str, Any]:
        cfg = self.config
        params: dict[str, Any] = {
            **cfg.options,
            "host": cfg.host,
            "port": cfg.port,
            "dbname": cfg.database,
            "connect_timeout": max(1, math.ceil(cfg.connect_timeout)),
            "sslmode": cfg.sslmode,
            "application_name": cfg.application_name,
        }
        if cfg.sslrootcert:
            params["sslrootcert"] = cfg.sslrootcert
        if isinstance(credentials, UsernamePassword):
            params["user"] = credentials.username
            params["password"] = credentials.password.get_secret_value()
        elif isinstance(credentials, ClientCertificate):
            params["user"] = credentials.username
            params["sslcert"] = credentials.cert_path
            params["sslkey"] = credentials.key_path
            if credentials.key_password is not None:
                params["sslpassword"] = credentials.key_password.get_secret_value()
        return params

    def _create_native(self, credentials: Credentials) -> psycopg.Connection[Any]:
        psycopg_mod = import_driver("psycopg", extra="postgres")
        # Keyword params (not a DSN string) so the password never sits in a conninfo string we build.
        conn: psycopg.Connection[Any] = psycopg_mod.connect(**self._connect_params(credentials))
        return conn

    def _native_id(self, native: psycopg.Connection[Any]) -> object:
        return native.info.backend_pid  # matches pg_stat_activity.pid on the server

    def _ping(self, native: psycopg.Connection[Any]) -> None:
        native.execute("SELECT 1")
        if not native.autocommit:
            native.rollback()

    def _reset_native(self, native: psycopg.Connection[Any]) -> bool:
        """End any open transaction and restore session defaults so the next caller starts clean."""
        if native.closed or native.broken:
            return False
        TransactionStatus = import_driver("psycopg.pq", extra="postgres").TransactionStatus  # noqa: N806

        status = native.info.transaction_status
        if status in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
            native.rollback()
        elif status != TransactionStatus.IDLE:  # ACTIVE (query in flight) or UNKNOWN (bad connection)
            return False
        if native.autocommit:
            native.autocommit = False
        for attr in ("isolation_level", "read_only", "deferrable"):
            if getattr(native, attr) is not None:
                setattr(native, attr, None)
        return True
