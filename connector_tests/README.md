# Connector connection tests

Checks that each connector, given the connection info in `connectors_test.yaml`, hands back a **live native
connection object**. It then writes an HTML report plus JSON files. The object details are also written
on their own, for the integration layer.

The harness calls the real SDK code (`enterprise_connectors`). It only connects, runs a health check and
closes; it never reads data.

## Setup (Windows / PowerShell)

No virtualenv. Install into the global Python:

```powershell
py -m pip install --user pyyaml "psycopg[binary]" pytest
```

Secrets come from `env://` references, resolved from `--env-file` first and then the process environment.
Values from the env file stay in memory and are never exported or written out.

## Run

```powershell
# from the repo root
py connector_tests\run_connector_tests.py --env-file .env
py connector_tests\run_connector_tests.py --env-file .env.local --only postgres --no-open
```

| Option | Meaning |
|---|---|
| `-c, --config FILE` | Use a different YAML file (default: `connectors_test.yaml`) |
| `--env-file FILE` | `KEY=VALUE` file used to resolve `env://` references |
| `--only TEXT` | Run only tests whose name contains TEXT (case-insensitive) |
| `--out DIR` | Report directory (default: `connector_tests\test_reports`) |
| `--no-open` | Don't open the HTML report in the browser |

The exit code is `0` when no test failed, and `1` otherwise (including YAML errors or no matching tests).

Each run writes three timestamped files to `test_reports\` (git-ignored):

- `connector_report_*.html`: a self-contained report built from `report_template.html`. It works offline, in light or dark mode, and on phones. It is a testing aid only. It has four tabs:
  - **Tests & results**: summary tiles, Pass/Fail/Skip filters and search; for each test, why it passed or failed, the expectation, the health check and any traceback.
  - **Connections**: the masked connection info (config, credentials, how the factory was called) and the returned object (type, repr, attributes, and nested objects such as psycopg's `info`).
  - **Connection pool**: from `pool_probe`. Shows the pool identity, pool config, and pool counts during and after each checkout, and whether the connection was reused.
  - **Pool connections**: how many Postgres connections the SDK opened across the run (from `event_hook`), and the concurrent-users probe. It shows N simulated users asking for a connection at once, how many real server connections served them (by backend PID), the peak number in use, server-side session count, wait times, a timeline, and SDK event counts per test.
  - **Flow charts**: each connector's path from start to end, from `flows.yaml`, with a badge where each test stopped or finished.
- `connector_results_*.json`: every result, including masked connection info, reason and traceback.
- `connection_objects_*.json`: **for the integration layer.** For each connection: full type name, repr, and public non-callable attributes (two levels deep by default; `capture_depth`). For Postgres, this includes `info.host`, `port`, `dbname`, `server_version`, `backend_pid` and the redacted `dsn`.

### CI (pytest)

```powershell
$env:CONNECTOR_TESTS_ENV_FILE = ".env"
py -m pytest connector_tests\test_connectors_pytest.py -v
```

There is one pytest case per YAML test, using the same logic, so its counts match the CLI. `CONNECTOR_TESTS_CONFIG` selects another YAML file.

## What PASS and FAIL mean

**Positive test** (`expect.success: true`, the default) passes only if all of these hold:
1. The factory and `connect_method` return an object that is not `None`.
2. `expect.object_type` matches, if set. `"module:Class"` is checked with `isinstance`; a plain name must match a class name, or a full `module.Class` name, in its MRO.
3. `health_check` doesn't raise or return `False`, if set. For Postgres this is `conn.execute("SELECT 1")`.
4. All of this finishes within `timeout_seconds`.

**Negative test** (`expect.success: false`) passes only if the connection fails **and**:
- the reason contains `expect.error_contains` (a string, or a list where any entry may match), and/or
- the failure category equals `expect.category`, for example `timeout`.

A negative test never passes because of a *setup* problem (unresolved secret, import error, invalid test
definition) unless that category is explicitly expected.

**Failure categories**

| Category | Meaning |
|---|---|
| `secret not resolved` | An `env://` variable is unset, or a secret scheme other than `env://` was used |
| `import error` | The factory or `credentials_class` could not be imported |
| `invalid test definition` | Bad `call_style`, missing factory, bad timeout, and so on |
| `exception` | The factory or connect raised; the traceback is in the report |
| `timeout` | No result within `timeout_seconds`. The attempt runs in a daemon thread, so it can't block the run or process exit. |
| `returned None` | Connect returned `None` |
| `type mismatch` | The object is not `expect.object_type` |
| `health check failed` | The health check raised or returned `False` |
| `unexpected success` / `wrong error` | A negative test connected, or failed for a different reason |

Close methods (`close_method` on the connection, `handle_close_method` on the connector, and `close()` on
any factory owner such as the `ConnectorManager`) always run afterwards. Errors from them are ignored.

## Adding a connector test

Add an entry under `tests:`:

```yaml
  - name: mysql-primary               # unique; used by --only
    type: mysql                       # connector_type passed to the factory
    enabled: true
    overrides: {timeout_seconds: 10}  # optional: any key from `defaults`
    config:                           # non-secret settings; env:// is allowed
      host: env://MYSQL_HOST
      port: env://MYSQL_PORT?type=int&default=3306
      database: env://MYSQL_DB
    credentials:                      # built with `credentials_class`
      username: env://MYSQL_USER
      password: env://MYSQL_PASSWORD
    expect:
      success: true
      object_type: "mysql.connector.abstracts:MySQLConnectionAbstract"
```

`env://NAME?type=int|float|bool&default=VALUE` casts the value and provides a fallback. Only use a default
for values that aren't secret.

**Call styles** (`defaults.call_style`, or per test in `overrides`):

| Style | Call |
|---|---|
| `type_config_credentials` | `factory(type, config, credentials_class(**credentials))`. This is the SDK style. |
| `type_and_config` | `factory(type, {**config, **credentials})` |
| `dict` | `factory({"type": type, **config, **credentials})` |
| `kwargs` | `factory(**config, **credentials)` |

`factory` is `module:callable`. A path segment ending in `()` is instantiated with no arguments for each
test and closed afterwards, as in `enterprise_connectors:ConnectorManager().create`.

## Testing the application's connections file

`connections_file:` in the test YAML points at a connections file (by default the repo's `connections.yaml`).
**Every entry becomes a test**: the harness loads the file with `ConnectorManager.from_yaml`, exactly as an
app would, then gets each connection by name and connects. An entry that is invalid, or can't connect, shows
as FAIL with its reason in the report and console. The other entries still run.

```yaml
connections_file:
  path: ../connections.yaml
  expect_by_type: {postgres: {object_type: "psycopg:Connection"}}
  tests:                                   # optional per-connection overrides / expectations
    legacy_db: {expect: {success: false, error_contains: "timed out"}}
  overrides: {timeout_seconds: 30}         # optional, applied to every generated test
```

## Pool probe

When `defaults.pool_probe` is set and the raw connection succeeded, the harness also checks out
`checkouts` pooled connections through `context_method` (e.g. `connector.connection()`). It runs the health
check inside each checkout and records `stats` (e.g. `owner.pool_manager.stats()`) during and after each one.
A failure there fails the test with category `pool check failed`.

## Concurrent users probe and event counts

`defaults.event_hook` (e.g. `owner.add_hook`) registers a callback on the SDK's telemetry hooks, so each test
records the events it caused (`connection.established`, `pool.checkout`, …).

A test's `overrides.concurrency_probe` starts `users` threads together. Each one checks out a pooled connection
through `context_method`, runs `identify` inside it, holds it for `hold_seconds`, then returns it. With more users
than the pool's `max_size`, some users wait for a returned connection, which is what the probe demonstrates.
`identify` + `columns` label the row (for Postgres: backend PID, user, database and server session count), and
`connection_key` says which column identifies a real server connection. If any user gets no connection, the test
fails with `pool check failed`. The harness closes its raw test connection before the probes, so server-side
counts reflect only pooled sessions.

## Flow charts

`flows.yaml` describes each connector's real code path in plain language, keyed by test `type`. Each box can have:
- `does`: what it does, in everyday words
- `input` and `output`
- `fails`: what can go wrong, and what you see
- `notes`
- `code`: the matching call
- `kind`: `step`, `decision`, `harness` or `result`

Stages can have a `summary`. `stop` rules (`reason` regex, `category`, or `success: true`) place a test's badge on
the box where it ended. Update the file whenever the code path changes.

Avoid writing `password: value`-style text in it: the report masks anything shaped like a secret. A missing or
invalid file only leaves the tab empty, and the run prints a warning.

## Secret handling

- Displayed connection info shows `env://` references, never resolved values. Literal values under keys
  containing `password`, `secret`, `token`, `key`, `credential`, `pwd` or `auth` are masked.
- Every resolved secret value is removed from reasons, tracebacks, reprs and captured attributes. The final
  HTML and JSON also get one more pass against every secret-named value in the env file.
- Because masking removes the exact value everywhere, a weak password that is also an ordinary word (for
  example, one equal to the username or the database product name) is masked wherever that word appears.
