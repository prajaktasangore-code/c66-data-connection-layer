# Using the connections: guide for the integration layer

This SDK gives you **live, pooled, native database connections**. Your code uses them to fetch data.
For PostgreSQL that is a `psycopg.Connection` (psycopg 3). The SDK never fetches data itself. Querying,
paging and transforming are your code's job, using the psycopg API.

## 1. Setup (Windows / PowerShell)

```powershell
# from your project, install this library and the Postgres driver (no virtualenv needed)
py -m pip install --user -e "C:\path\to\c66-data-connection-layer[postgres,yaml]"
```

Extras: `postgres` (psycopg 3), `yaml` (read connections.yaml), `aws` (`awssm://` secret references).

Create a `.env` from `.env.example` and fill in the real values. `.env` is git-ignored; never commit it.

```dotenv
EC_POSTGRES_HOST=localhost
EC_POSTGRES_PORT=5433
EC_POSTGRES_DATABASE=data_pipeline_agent
EC_POSTGRES_USER=...
EC_POSTGRES_PASSWORD=...
```

The library itself never reads `.env`. Put those variables into the environment the way your app normally
does, for example with `python-dotenv` (`load_dotenv()`), your process manager, or PowerShell
`$env:EC_POSTGRES_PASSWORD = "..."`. You can also pass a mapping with `environ=` (see below).

## 2. Declare connections in `connections.yaml`

The repo root has a ready `connections.yaml` with one connection, `pipeline_db`. Add more entries the same way:

```yaml
maintenance_interval: 30            # background health checks + stale-connection recycling (seconds)

connections:
  pipeline_db:
    type: postgres
    config:
      host: env://EC_POSTGRES_HOST
      port: env://EC_POSTGRES_PORT      # converted to int
      database: env://EC_POSTGRES_DATABASE
      application_name: c66-integration-layer
    credentials:
      username: env://EC_POSTGRES_USER
      password: env://EC_POSTGRES_PASSWORD     # secrets MUST be references
    pool: {min_size: 1, max_size: 10, checkout_timeout: 15}

  reporting_db:
    type: postgres
    config: {host: reports.internal, database: reporting, sslmode: verify-full}
    credentials:
      username: report_reader
      password: awssm://prod/reporting#password   # AWS Secrets Manager, needs the [aws] extra
```

- **Secret references:** `env://NAME`, and `awssm://SECRET_ID#json_key` (optionally `?region=...`). They are
  resolved **each time a connection is opened**, so rotated secrets are picked up and nothing secret sits in
  memory. `vault://` and `akv://` are recognised but not supported yet.
- A plain-text password in the file is rejected.
- **Entries load independently.** An invalid entry is skipped and recorded in `manager.load_errors`; the
  other connections work. `manager.get("bad_entry")` raises with the reason. Use
  `from_yaml(..., strict=True)` if you would rather reject the whole file.
- No connection is opened when the file loads. The first `connection()` opens it.

## 3. Use it: once at startup, then everywhere

```python
from enterprise_connectors import ConnectorManager, ConnectorError

# Application startup: create ONE manager for the life of the process.
manager = ConnectorManager.from_yaml("connections.yaml")
for name, reason in manager.load_errors.items():
    print(f"connection {name} unavailable: {reason}")  # or log/alert; others still work

pipeline_db = manager.get("pipeline_db")  # same object every time -> one shared pool


def fetch_active_customers() -> list[tuple]:
    # Borrow a pooled connection; it is returned (and cleaned) when the block exits.
    with pipeline_db.connection() as conn:  # psycopg.Connection
        return conn.execute("SELECT id, name FROM customers WHERE active = %s", (True,)).fetchall()


def fetch_in_batches(batch_size: int = 1000):
    with pipeline_db.connection() as conn:
        with conn.cursor(name="batch_cursor") as cur:  # server-side cursor: streams large results
            cur.execute("SELECT * FROM big_table")
            while rows := cur.fetchmany(batch_size):
                yield rows


def write_something() -> None:
    with pipeline_db.connection() as conn:
        conn.execute("UPDATE jobs SET status = %s WHERE id = %s", ("done", 42))
        conn.commit()  # uncommitted work is rolled back on return


# Application shutdown
manager.close()
```

Rules of thumb:
- **Create the manager once** and keep it. Creating one per request throws the pool away each time.
- **Always use `with ... connection() as conn`**, and don't keep `conn` after the block; it belongs to the pool.
- Up to `max_size` callers (threads) can hold a connection at once; the rest wait up to `checkout_timeout`.
  The report's *Pool connections* tab shows 10 concurrent users being served by 5 connections.
- Transactions are yours: `commit()` explicitly. Open or failed transactions are rolled back when the
  connection goes back to the pool, and session settings (`autocommit`, `read_only`, isolation) are reset.
- `connector.connect()` gives a raw, unpooled connection you must `close()` yourself. Use it only for one-off scripts.

### Without a YAML file

```python
from enterprise_connectors import ConnectorManager, EnvCredentialProvider

manager = ConnectorManager()
pipeline_db = manager.create(
    "postgres",
    {"host": "localhost", "port": 5433, "database": "data_pipeline_agent"},
    EnvCredentialProvider.username_password("EC_POSTGRES"),  # reads EC_POSTGRES_USER / _PASSWORD
    pool_config={"min_size": 1, "max_size": 10},
)
```

### Passing environment values explicitly

```python
manager = ConnectorManager.from_yaml("connections.yaml", environ={"EC_POSTGRES_HOST": "...", ...})
```

## 4. Errors

All failures raise one of these. Messages never contain secrets.

| Exception | Typical cause |
|---|---|
| `ConfigurationError` | Bad config, missing env var, unknown connection name, driver not installed |
| `AuthenticationError` | Wrong credentials, secret could not be read |
| `AuthorizationError` | Logged in but not permitted |
| `ConnectionError` (also a builtin `ConnectionError`) | Host unreachable |
| `ConnectionTimeout` (also a `TimeoutError`) | Connect took longer than `connect_timeout` |
| `PoolTimeout` / `PoolExhausted` | All pooled connections busy for longer than `checkout_timeout` |

```python
try:
    with pipeline_db.connection() as conn:
        ...
except ConnectorError as exc:  # catch-all for SDK errors; psycopg errors from your SQL pass through as-is
    log.error("database unavailable: %s", exc)
```

## 5. Health and monitoring

```python
ok = pipeline_db.test_connection()  # bool; details in pipeline_db.last_result (ok, latency_ms, error_type)
stats = manager.pool_manager.stats()  # {PoolKey: PoolStats(total, idle, in_use, waiting, max_size)}
manager.add_hook(lambda event: print(event.name, event.success, event.duration_ms))  # connect/checkout/... events
# every event's attributes include connection_name (from connections.yaml), connection_id (Postgres backend PID)
# and, on pool.checkout / pool.return, a lease_id pairing the two. See HOW_TO_CONNECT_AND_FETCH_DATA.md#logging-connections
```

## 6. Checking everything works

```powershell
py connector_tests\run_connector_tests.py --env-file .env   # tests every connection in connections.yaml + report
py -m pytest                                                # unit + integration tests (integration reads .env)
```

The HTML report shows each connection's result and reason, the returned object, pool behaviour under concurrent
users, and a flow chart. `test_reports\connection_objects_*.json` lists the exact attributes of the connection
object you will receive.
