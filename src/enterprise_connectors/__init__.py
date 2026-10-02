"""Enterprise Connection SDK: secure connection management returning native clients."""

import logging as _logging

from .core.base import BaseConnector
from .core.connection_config import ConnectionConfig
from .core.connection_result import ConnectionResult
from .core.credentials import (
    ApiKeyCredential,
    AwsSecretsManagerProvider,
    ClientCertificate,
    CredentialProvider,
    Credentials,
    EnvCredentialProvider,
    OAuthClientCredentials,
    ReferenceCredentialProvider,
    SecretStr,
    StaticCredentialProvider,
    TokenCredential,
    UsernamePassword,
)
from .core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ConnectionError,  # noqa: A004 - name fixed by spec
    ConnectionPoolError,
    ConnectionTimeout,
    ConnectorError,
    PoolExhausted,
    PoolTimeout,
    UnsupportedOperation,
)
from .manager.connector_manager import ConnectorManager
from .pooling.pool_config import ExhaustionPolicy, PoolConfig
from .registry.connector_registry import ConnectorRegistry
from .utils.logging import ConnectionEvent, Events
from .utils.retry import RetryPolicy

_logging.getLogger(__name__).addHandler(_logging.NullHandler())

__version__ = "0.1.0"

__all__ = [
    "ApiKeyCredential",
    "AuthenticationError",
    "AuthorizationError",
    "AwsSecretsManagerProvider",
    "BaseConnector",
    "ClientCertificate",
    "ConfigurationError",
    "ConnectionConfig",
    "ConnectionError",
    "ConnectionEvent",
    "ConnectionPoolError",
    "ConnectionResult",
    "ConnectionTimeout",
    "ConnectorError",
    "ConnectorManager",
    "ConnectorRegistry",
    "CredentialProvider",
    "Credentials",
    "EnvCredentialProvider",
    "Events",
    "ExhaustionPolicy",
    "OAuthClientCredentials",
    "PoolConfig",
    "PoolExhausted",
    "PoolTimeout",
    "ReferenceCredentialProvider",
    "RetryPolicy",
    "SecretStr",
    "StaticCredentialProvider",
    "TokenCredential",
    "UnsupportedOperation",
    "UsernamePassword",
]
