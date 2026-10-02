# enterprise-connectors

Enterprise Connection SDK: a standalone Python package that securely configures, authenticates,
establishes, pools, health-checks and closes connections to enterprise sources, and gives you the
source's **native** connection or client (for PostgreSQL, a `psycopg.Connection`).

It does not fetch, transform, paginate or load data. Its job ends when a working native connection is
available; your code queries with it.

**Status:** PostgreSQL is implemented and certified. More sources (MySQL, Snowflake, S3, MongoDB, Google Drive, …) are planned.

## Quick start (Windows / PowerShell)

```powershell
py -m pip install --user -e ".[postgres,yaml]"     # from this folder; no virtualenv needed
Copy-Item .env.example .env                         # then put the real values in .env (git-ignored)
```

```python
from enterprise_connectors import ConnectorManager

manager = ConnectorManager.from_yaml("connections.yaml")  # once, at application startup
print(manager.load_errors)  # entries that could not be loaded, with reasons

with manager.get("pipeline_db").connection() as conn:  # pooled psycopg.Connection
    rows = conn.execute("SELECT ...").fetchall()

manager.close()  # at shutdown
```

`connections.yaml` declares named connections. Secrets are only references (`env://NAME`,
`awssm://SECRET_ID#key`) and are resolved at connect time. The library never reads `.env` itself: load it
into the environment in your app (e.g. `python-dotenv`), or pass `environ=` to `from_yaml`.

**→ New here? Start with [HOW_TO_CONNECT_AND_FETCH_DATA.md](HOW_TO_CONNECT_AND_FETCH_DATA.md)** (simple steps, inputs/outputs, examples).
Full reference for application developers: [docs/examples.md](docs/examples.md)

| Doc | Contents |
|---|---|
| [docs/examples.md](docs/examples.md) | Setup, connections.yaml, using pooled connections, errors, monitoring |
| [docs/pooling.md](docs/pooling.md) | Pool lifecycle and every pool setting |
| [docs/architecture.md](docs/architecture.md) | Components and guarantees (secrets, isolation, retries) |
| [docs/connector-development.md](docs/connector-development.md) | How to add a new source connector |
| [connector_tests/README.md](connector_tests/README.md) | Live connection test harness and HTML report (testing aid) |

## Extras

| Extra | Adds |
|---|---|
| `postgres` | psycopg 3 driver |
| `yaml` | `ConnectorManager.from_yaml` |
| `aws` | `awssm://` secret references / `AwsSecretsManagerProvider` |
| `oauth` | OAuth token client (requests) |
| `dev` | pytest, ruff, mypy, PyYAML |

## Handing the package to other developers

Other developers get the **package, not this repo**. Build the handoff bundle:

```powershell
powershell -ExecutionPolicy Bypass -File scriptsuild_package.ps1
```

This writes `dist\enterprise-connectors-<version>\` and a `.zip` of it, containing:
- the wheel
- `HOW_TO_CONNECT_AND_FETCH_DATA.md`
- `connections.example.yaml`
- `.env.example`

Tests, the harness, docs, reports and `.env` are never included. The script checks this, and stops if the
wheel holds anything but the package or a real secret appears in the bundle. They install it with
`py -m pip install --user "<path>\enterprise_connectors-<version>-py3-none-any.whl[postgres,yaml]"`.

For a new release, bump `version` in `pyproject.toml` and run the script again. To publish to a private
package index instead of sending files, upload the same wheel (e.g. with `twine`).

## Checking it works

```powershell
py connector_tests\run_connector_tests.py --env-file .env   # every connection in connections.yaml + HTML report
py -m pytest                                                # unit, contract, concurrency, security, integration
py -m ruff check .; py -m mypy src
```

Integration tests read `.env` automatically and skip when it is absent.

## Known environment notes (this dev machine)

- The local PostgreSQL 18 service listens on port **5433**, not 5432.
- Its `pg_hba.conf` uses `trust` authentication, so any password is accepted. The wrong-password
  integration test skips itself in that case. Switch to `scram-sha-256` to test real authentication.
