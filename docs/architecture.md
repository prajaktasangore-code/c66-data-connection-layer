# Architecture

Source of truth: `Enterprise_Connection_SDK_Final_Architecture.pdf`. The SDK configures,
authenticates, establishes, pools, health-checks and closes connections, and hands back the source's
**native** client. It does not fetch, transform or load data.

```
connections.yaml / dict config + credentials
        │
        ▼
ConnectorManager ──► ConnectorRegistry ──► PostgresConnector (lazy import)
        │                                      │
        │                         validate() → authenticate() → connect() / connection()
        │                                      │
        ▼                                      ▼
   PoolManager ──── PoolKey ────►  ConnectionPool ──► psycopg.connect(...)  ──►  psycopg.Connection
                                   (checkout / return / health / recycle)
```

| Package | Responsibility |
|---|---|
| `core/base.py` | `BaseConnector`: the public lifecycle (`validate`, `authenticate`, `connect`, `test_connection`, `connection`, `close`) plus the hooks connectors implement (`_create_native`, `_ping`, `_reset_native`, `_close_native`) |
| `core/connection_config.py` | Frozen, keyword-only config dataclasses with no secrets; `env://` resolution for config |
| `core/credentials.py` | `SecretStr`, credential types, providers (static, env, AWS Secrets Manager, references) |
| `core/exceptions.py` | The spec's exception hierarchy; `map_driver_error` classifies and redacts driver errors |
| `manager/connector_manager.py` | Public entry point: `create`, `from_yaml` / `from_config`, `get`, `close` |
| `registry/` | `connector_type` → class, lazily imported so drivers are optional |
| `pooling/` | Generic thread-safe pool, pool keys, health checks, background maintenance |
| `connectors/databases/postgres.py` | psycopg 3 connector (reference implementation) |
| `utils/` | Redacting logger + telemetry hooks, validation helpers, establishment retry |

Key guarantees:
- **Secrets never in logs or errors.** `SecretStr` values never render, driver messages are pattern-redacted
  and scrubbed of the exact secret values in use, and raw driver exceptions are not chained.
- **Isolation.** The pool key includes the connector type, non-secret config identity, tenant, a keyed credential
  fingerprint and the pool config, so different users, tenants or credentials never share a connection.
- **Lazy and rotating credentials.** Credentials are fetched on first use. After an authentication failure
  they are re-fetched once; changed credentials get a fresh pool.
- **Retries** only for unreachable or timed-out establishment (3 attempts, exponential backoff), never for
  auth or config errors.
- **Optional dependencies.** The core has none; `[postgres]`, `[yaml]` and `[aws]` are extras.

