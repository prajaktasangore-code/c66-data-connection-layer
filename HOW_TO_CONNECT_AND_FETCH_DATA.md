# How to connect to a database and fetch data with `enterprise-connectors`

This package gives your code a **ready-to-use database connection**. You tell it *which* database (in a YAML
file) and *where the password is* (an environment variable). It hands you back a live connection, and you use
that connection to run your SQL and read the results.

```
 your settings                    this package                               your code
┌────────────────────┐     ┌──────────────────────────────┐     ┌──────────────────────────────────┐
│ connections.yaml   │ ──► │ checks the settings          │ ──► │ conn.execute("SELECT ...")       │
│ .env (passwords)   │     │ logs in, keeps a pool of     │     │ .fetchall()  → your data         │
└────────────────────┘     │ open connections, reconnects │     └──────────────────────────────────┘
                           └──────────────────────────────┘
```

What it does for you:
- logs in securely (passwords are never printed or logged)
- keeps connections open and **reuses** them (a *pool*), so your code is fast and doesn't overload the database
- checks connections are healthy, replaces broken ones and retries if the database is briefly unreachable
- gives clear error messages when something is wrong

What it does **not** do: it does not run your queries for you. The `SELECT` and the `fetch` are your code,
using the connection it gives you. Supported today: **PostgreSQL**.

**Contents**
1. [What you received](#what-you-received)
2. [Inputs and outputs at a glance](#inputs-and-outputs-at-a-glance)
3. [Before you start (checklist)](#before-you-start-checklist)
4. Steps:
   - [1 Unzip](#step-1-unzip-the-bundle)
   - [2 Install](#step-2-install-the-package)
   - [3 Project folder](#step-3-set-up-your-project-folder)
   - [4 .env](#step-4-put-the-passwords-in-env)
   - [5 connections.yaml](#step-5-describe-your-databases-in-connectionsyaml)
   - [6 Check](#step-6-check-the-connections)
   - [7 Fetch data](#step-7-write-your-code-connect-and-fetch-data)
5. [More examples](#more-examples)
6. [Do's and don'ts](#dos-and-donts)
7. [When something goes wrong](#when-something-goes-wrong)
8. [Troubleshooting install and setup](#troubleshooting-install-and-setup)
9. [Logging connections](#logging-connections)
10. [Adding another database](#adding-another-database)
11. [Upgrading, uninstalling and other database types](#upgrading-uninstalling-and-other-database-types)
12. [Command cheat sheet](#command-cheat-sheet)

---

## What you received

A folder (or `.zip`) named `enterprise-connectors-<version>`, for example `enterprise-connectors-0.1.0`:

| File | What it is | What you do with it |
|---|---|---|
| `enterprise_connectors-<version>-py3-none-any.whl` | The package itself (a "wheel") | Install it (Step 2) |
| `HOW_TO_CONNECT_AND_FETCH_DATA.md` | This guide | Read it |
| `connections.example.yaml` | A sample list of databases | Copy it into your project as `connections.yaml` (Step 5) |
| `.env.example` | A template for passwords and other settings | Copy it into your project as `.env` (Step 4) |

> The file names contain the version (e.g. `0.1.0`). In the commands below, replace `0.1.0` with the
> version you received.

---

## Inputs and outputs at a glance

### What you must provide (inputs)

| # | Input | Where | Example |
|---|---|---|---|
| 1 | Python 3.10 or newer | your machine | `py --version` → `Python 3.12.4` |
| 2 | The package and the PostgreSQL driver | installed once, from the `.whl` file | Step 2 |
| 3 | **Connection settings**: host, port, database name | `.env` + `connections.yaml` | `EC_POSTGRES_HOST=db.mycompany.local` |
| 4 | **Login**: username and password | `.env` (never committed to git) | `EC_POSTGRES_PASSWORD=...` |
| 5 | A **name** for each database | `connections.yaml` | `pipeline_db` |
| 6 | Your SQL query | your code | `"SELECT id, name FROM customers"` |

### What you get back (outputs)

| Step | You call | You get |
|---|---|---|
| Load settings | `ConnectorManager.from_yaml("connections.yaml")` | a **manager** that knows all your databases |
| Pick a database | `manager.get("pipeline_db")` | a **connector** for that database |
| Borrow a connection | `with db.connection() as conn:` | **`conn`**, a live `psycopg.Connection` (PostgreSQL) |
| Run SQL | `conn.execute("SELECT ...")` | a cursor holding the result |
| Read rows | `.fetchall()` / `.fetchone()` / `.fetchmany(n)` | **your data**: a list of rows (tuples, or dicts if you ask) |
| Check problems | `manager.load_errors` | `{name: reason}` for any database whose settings were wrong |
| Test a database | `db.test_connection()` | `True` / `False`, with details in `db.last_result` |

---

## Before you start (checklist)

All commands in this guide are for **Windows PowerShell**. Open it from the Start menu, or use the terminal
in VS Code.

**1. Python 3.10 or newer is installed**

```powershell
py --version
```

Expected: `Python 3.10.x` or newer. If you see *"'py' is not recognized"*, install Python from
<https://www.python.org/downloads/>. In the installer, tick **"Add python.exe to PATH"** and keep
**"py launcher"** selected. Then open a **new** PowerShell window and try again.

**2. pip works**

```powershell
py -m pip --version
```

Expected: `pip 23.x ... (python 3.x)` or newer.

**3. You can download from the internet (PyPI)**

The PostgreSQL driver (`psycopg`) and YAML support are downloaded during install. If your company uses a
proxy, see [Troubleshooting](#troubleshooting-install-and-setup).

**4. You have the database details**

Ask the database owner for: **host**, **port**, **database name**, **username** and **password**.

**5. Your machine can reach the database** (replace host and port with yours)

```powershell
Test-NetConnection db.mycompany.local -Port 5432
```

Expected: `TcpTestSucceeded : True`. If it says `False`, the database is unreachable from your machine
(wrong host or port, VPN off, or firewall). Fix that first, because no code will connect until it does.

> Using `localhost` on a developer machine? The connector team's local PostgreSQL runs on port **5433**, not
> the usual 5432. Check `Test-NetConnection localhost -Port 5433`.

---

## Step-by-step

### Step 1: Unzip the bundle

Skip this if you received a folder rather than a `.zip`.

```powershell
Expand-Archive -Path "$HOME\Downloads\enterprise-connectors-0.1.0.zip" -DestinationPath "C:\packages\enterprise-connectors-0.1.0"
Get-ChildItem -Force "C:\packages\enterprise-connectors-0.1.0"
```

You should see the four files from [What you received](#what-you-received). `-Force` also shows `.env.example`,
which starts with a dot. The commands below assume the bundle is in `C:\packages\enterprise-connectors-0.1.0`.

### Step 2: Install the package

```powershell
py -m pip install --user "C:\packages\enterprise-connectors-0.1.0\enterprise_connectors-0.1.0-py3-none-any.whl[postgres,yaml]"
py -m pip install --user python-dotenv
```

- **Keep the quotes.** The text in square brackets, `[postgres,yaml]`, is part of the command. It also
  installs the PostgreSQL driver (`psycopg`) and YAML support (`PyYAML`).
- `python-dotenv` lets your program read the `.env` file (Step 4).
- `--user` installs for your Windows user only, so no administrator rights are needed. Inside a virtual
  environment, leave `--user` out (see below).
- A message like *"WARNING: The script … is installed in … which is not on PATH"* is harmless. You always
  run things with `py ...` in this guide.

**Check that it worked:**

```powershell
py -m pip show enterprise-connectors
py -c "import enterprise_connectors, psycopg, yaml, dotenv; print('enterprise-connectors', enterprise_connectors.__version__, 'is ready')"
```

Expected: package details (`Name: enterprise-connectors`, `Version: 0.1.0`), then
`enterprise-connectors 0.1.0 is ready`.

**What the square-bracket "extras" mean**

| Extra | Installs | You need it when |
|---|---|---|
| `postgres` | psycopg 3 (PostgreSQL driver) | you connect to PostgreSQL |
| `yaml` | PyYAML | you use `connections.yaml` (recommended) |
| `aws` | boto3 | your passwords are in AWS Secrets Manager (`awssm://`) |
| `oauth` | requests | you use the OAuth token helpers |

Combine extras with commas, e.g. `[postgres,yaml,aws]`. Future versions will add one extra per new database
type (see [Other database types](#upgrading-uninstalling-and-other-database-types)).

**Optional: install into a virtual environment instead**

If your project uses a virtual environment, activate it first and install without `--user`:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install "C:\packages\enterprise-connectors-0.1.0\enterprise_connectors-0.1.0-py3-none-any.whl[postgres,yaml]" python-dotenv
```

**Optional: add it to your project's `requirements.txt`**

Use forward slashes and three slashes after `file:`:

```text
enterprise-connectors[postgres,yaml] @ file:///C:/packages/enterprise-connectors-0.1.0/enterprise_connectors-0.1.0-py3-none-any.whl
python-dotenv
```

Then: `py -m pip install --user -r requirements.txt`.

### Step 3: Set up your project folder

This is the layout used in the rest of this guide (your own code files can be anywhere):

```
C:\projects\my-app\
├── .env                     ← passwords and connection settings (Step 4), never commit this
├── .gitignore               ← must contain the line: .env
├── connections.yaml         ← your list of databases (Step 5)
├── check_connections.py     ← checks every database (Step 6)
└── app.py                   ← your code (Step 7)
```

```powershell
New-Item -ItemType Directory -Force C:\projects\my-app | Out-Null
Set-Location C:\projects\my-app
```

### Step 4: Put the passwords in `.env`

```powershell
Copy-Item "C:\packages\enterprise-connectors-0.1.0\.env.example" .env
notepad .env
```

Fill in your real values and save:

```dotenv
EC_POSTGRES_HOST=db.mycompany.local
EC_POSTGRES_PORT=5432
EC_POSTGRES_DATABASE=data_pipeline_agent
EC_POSTGRES_USER=your_user
EC_POSTGRES_PASSWORD=your_password
```

| Variable | Meaning | Example |
|---|---|---|
| `EC_POSTGRES_HOST` | Server name or IP address (no `postgres://`, no port) | `db.mycompany.local`, `10.0.0.12`, `localhost` |
| `EC_POSTGRES_PORT` | Port number | `5432` (standard), `5433` (connector team's local machine) |
| `EC_POSTGRES_DATABASE` | Database name | `data_pipeline_agent` |
| `EC_POSTGRES_USER` | Login name | `report_reader` |
| `EC_POSTGRES_PASSWORD` | Login password | your password |

Rules for this file:
- one `NAME=value` per line, with no spaces around `=` and no quotes needed
- lines starting with `#` are comments
- you can rename the variables, as long as `connections.yaml` uses the same names

**Keep `.env` out of git:**

```powershell
if (-not (Test-Path .gitignore) -or -not (Select-String -Path .gitignore -Pattern '^\.env$' -Quiet)) { Add-Content .gitignore ".env" }
git check-ignore .env        # prints ".env" if it is ignored (only works inside a git repository)
```

### Step 5: Describe your databases in `connections.yaml`

```powershell
Copy-Item "C:\packages\enterprise-connectors-0.1.0\connections.example.yaml" connections.yaml
notepad connections.yaml
```

Each database gets a **name** (`pipeline_db` here). That is the name you use in code:

```yaml
maintenance_interval: 30               # optional: re-check idle connections every 30 seconds

connections:
  pipeline_db:                         # the name you will use in code
    type: postgres
    config:
      host: env://EC_POSTGRES_HOST     # env://NAME = "read this value from the environment / .env"
      port: env://EC_POSTGRES_PORT
      database: env://EC_POSTGRES_DATABASE
      application_name: my-app         # optional: shows up on the database server, so DBAs can see who is connected
    credentials:
      username: env://EC_POSTGRES_USER
      password: env://EC_POSTGRES_PASSWORD   # passwords MUST be env:// (or awssm://), never typed in
    pool:
      min_size: 1                      # keep 1 connection open and ready
      max_size: 10                     # at most 10 connections open at once
      checkout_timeout: 15             # wait at most 15 s for a free connection
```

> YAML is sensitive to indentation. Use **spaces, not tabs**, and keep the nesting exactly as shown.

**All settings you can use**

| Section | Setting | Required | Default | Meaning |
|---|---|---|---|---|
| — | `type` | yes | — | `postgres` |
| `config` | `host` | yes | — | server name or IP |
| `config` | `database` | yes | — | database name |
| `config` | `port` | no | `5432` | port number |
| `config` | `sslmode` | no | `prefer` | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full` |
| `config` | `sslrootcert` | no | — | path to the CA certificate (for `verify-ca` / `verify-full`) |
| `config` | `connect_timeout` | no | `10` | seconds to wait when connecting |
| `config` | `application_name` | no | `enterprise-connectors` | name shown on the database server |
| `config` | `retry` | no | 3 attempts | e.g. `retry: {max_attempts: 5}`: retries when the server can't be reached |
| `credentials` | `username` | yes | — | login name (plain text or `env://`) |
| `credentials` | `password` | yes | — | **must** be `env://NAME` or `awssm://SECRET_ID#key` |
| `pool` | `min_size` | no | `0` | connections kept open and ready |
| `pool` | `max_size` | no | `5` | most connections open at once |
| `pool` | `checkout_timeout` | no | `30` | seconds to wait for a free connection |
| `pool` | `max_lifetime` | no | `1800` | seconds before a connection is replaced with a fresh one |
| `pool` | `idle_timeout` | no | `600` | seconds an unused connection stays open |
| — | `maintenance_interval` (top level) | no | off | seconds between background health checks |

**Where passwords can come from**

| Reference | Reads from | Needs |
|---|---|---|
| `env://EC_POSTGRES_PASSWORD` | the environment / `.env` | nothing extra |
| `awssm://prod/pipeline-db#password` | key `password` of secret `prod/pipeline-db` in AWS Secrets Manager | the `aws` extra, and AWS credentials on the machine |
| `awssm://prod/pipeline-db#password?region=eu-west-1` | same, in a specific region | same |

Passwords are read **each time a connection is opened**, so a changed password is picked up without a restart.

### Step 6: Check the connections

Create `check_connections.py` in your project folder:

```python
from pathlib import Path

from dotenv import load_dotenv
from enterprise_connectors import ConnectorManager

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")                                       # read passwords from .env
manager = ConnectorManager.from_yaml(HERE / "connections.yaml")

for name, reason in manager.load_errors.items():
    print(f"FAIL  {name}: settings are invalid: {reason}")

for name in manager.names():
    db = manager.get(name)
    if db.test_connection():
        print(f"OK    {name}: connected in {db.last_result.latency_ms:.0f} ms (session {db.last_result.connection_id})")
    else:
        print(f"FAIL  {name}: {db.last_result.error_type}: {db.last_result.message}")

manager.close()
```

Run it:

```powershell
py check_connections.py
```

Expected output:

```
OK    pipeline_db: connected in 42 ms (session 53216)
```

Any `FAIL` line tells you what is wrong; see [When something goes wrong](#when-something-goes-wrong). Fix it
before continuing.

### Step 7: Write your code: connect and fetch data

Create `app.py`:

```python
from pathlib import Path

from dotenv import load_dotenv
from enterprise_connectors import ConnectorManager

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

# 1. Load your databases ONCE when your program starts.
manager = ConnectorManager.from_yaml(HERE / "connections.yaml")

# 2. See if any database settings were invalid (the others still work).
for name, reason in manager.load_errors.items():
    print(f"Database '{name}' is not available: {reason}")

# 3. Pick the database by its name.
db = manager.get("pipeline_db")

# 4. Borrow a connection, run your SQL, read the rows.
with db.connection() as conn:
    rows = conn.execute("SELECT table_name FROM information_schema.tables LIMIT 5").fetchall()

for row in rows:
    print(row)            # ('customers',)  ('orders',)  ...

# 5. When your program ends, close everything.
manager.close()
```

Run it:

```powershell
py app.py
```

That is all you need. The connection goes back to the pool automatically when the `with` block ends.

> **Why `HERE / ...`?** It finds `.env` and `connections.yaml` next to your script, whichever folder you run
> it from. Plain `"connections.yaml"` only works when PowerShell is in the project folder.

**Running in VS Code:** open the project folder, press `Ctrl+Shift+P` → *Python: Select Interpreter* and
choose the Python you installed the package into (the one `py` uses, or your `.venv`). Then press `F5` or
click **Run**.

---

## More examples

All examples assume `db = manager.get("pipeline_db")` from Step 7.

### Get rows as dictionaries (column name → value)

```python
from psycopg.rows import dict_row

with db.connection() as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT table_schema, table_name FROM information_schema.tables LIMIT 3")
        rows = cur.fetchall()

print(rows[0])   # {'table_schema': 'public', 'table_name': 'customers'}
```

### Pass values safely (never build SQL with f-strings)

```python
with db.connection() as conn:
    row = conn.execute(
        "SELECT * FROM customers WHERE country = %s AND active = %s",
        ("IN", True),                          # values go here, not inside the SQL text
    ).fetchone()                               # one row, or None
```

### Read a very large table in batches (doesn't load everything into memory)

```python
with db.connection() as conn:
    with conn.cursor(name="big_read") as cur:     # a named cursor streams from the server
        cur.execute("SELECT * FROM big_table")
        while batch := cur.fetchmany(1000):
            process(batch)                         # your code, 1000 rows at a time
```

### Write data (remember to commit)

```python
with db.connection() as conn:
    conn.execute("UPDATE jobs SET status = %s WHERE id = %s", ("done", 42))
    conn.commit()        # without commit(), the change is rolled back when the block ends
```

### Into a pandas DataFrame (if your project uses pandas)

```python
import pandas as pd
from psycopg.rows import dict_row

with db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
    cur.execute("SELECT * FROM customers")
    df = pd.DataFrame(cur.fetchall())
```

### Inside a web app or a long-running service

```python
# app startup (once)
manager = ConnectorManager.from_yaml(HERE / "connections.yaml")
db = manager.get("pipeline_db")

# in any request handler / job, as often as you like
def get_customer(customer_id: int):
    with db.connection() as conn:
        return conn.execute("SELECT * FROM customers WHERE id = %s", (customer_id,)).fetchone()

# app shutdown
manager.close()
```

Many threads can call `get_customer` at the same time. Each one gets its own connection from the pool. If all
10 are busy, the next caller waits up to 15 seconds for one to be returned.

### Without a YAML file

```python
from enterprise_connectors import ConnectorManager, EnvCredentialProvider

manager = ConnectorManager()
db = manager.create(
    "postgres",
    {"host": "db.mycompany.local", "port": 5432, "database": "data_pipeline_agent"},
    EnvCredentialProvider.username_password("EC_POSTGRES"),   # reads EC_POSTGRES_USER and EC_POSTGRES_PASSWORD
    pool_config={"max_size": 10},
)
```

---

## Do's and don'ts

| ✅ Do | ❌ Don't |
|---|---|
| Create the manager **once** at startup and reuse it | Create a new manager for every query (that throws the pool away) |
| Always use `with db.connection() as conn:` | Keep `conn` after the `with` block ends |
| Put passwords in `.env` and reference them with `env://` | Type a password into `connections.yaml` (it will be rejected) |
| Add `.env` to `.gitignore` | Commit `.env` or paste passwords into code, tickets or chat |
| Use `%s` placeholders for values | Build SQL with f-strings or `+` |
| Call `conn.commit()` after writes | Expect writes to be saved without `commit()` |
| Call `manager.close()` when the program ends | Leave the program without closing (connections are cut off) |

---

## When something goes wrong

Every problem raises a clear error. None of them ever contain your password.

| Error | What it usually means | What to check |
|---|---|---|
| `ConfigurationError` | Settings are wrong or missing | `connections.yaml` spelling, `.env` values, the connection name in `manager.get(...)` |
| `AuthenticationError` | Username/password rejected | `.env` username and password |
| `AuthorizationError` | Logged in, but not allowed | database permissions for that user (ask the database owner) |
| `ConnectionError` | Can't reach the database | host, port, VPN/firewall; run `Test-NetConnection <host> -Port <port>` |
| `ConnectionTimeout` | The database didn't answer in time | same as above; `connect_timeout` in config |
| `PoolTimeout` | All connections are busy | queries holding connections too long, or raise `pool: max_size` |

```python
from enterprise_connectors import ConnectorError

try:
    with db.connection() as conn:
        rows = conn.execute("SELECT 1").fetchall()
except ConnectorError as error:          # catches every error from this package
    print("Database problem:", error)
```

Errors in **your SQL** (e.g. a misspelled table) come straight from psycopg, such as `psycopg.errors.UndefinedTable`.

**Quick health check:**

```python
print(db.test_connection())          # True / False
print(db.last_result)                # details: ok, latency_ms, error_type, message, connection_id
```

---

## Troubleshooting install and setup

| You see | Cause | Fix |
|---|---|---|
| `'py' is not recognized as the name of a cmdlet` | Python or the py launcher isn't installed or on PATH | Install from python.org with "py launcher" ticked; open a **new** PowerShell |
| `ERROR: Package 'enterprise-connectors' requires a different Python` | Python older than 3.10 | Install Python 3.10+; check with `py --version` |
| `ERROR: ... is not a valid wheel filename` / `No such file` | Wrong path or file name | Check the path with `Get-ChildItem C:\packages\enterprise-connectors-0.1.0`; keep the quotes |
| `Could not find a version that satisfies ... psycopg` / SSL or proxy errors | No internet, or a company proxy | `py -m pip install --user --proxy http://proxy.mycompany.local:8080 "<wheel>[postgres,yaml]"`, or ask IT |
| `ModuleNotFoundError: No module named 'enterprise_connectors'` | Installed into a different Python than the one running your code | Run your code with the same `py` you installed with; in VS Code select that interpreter; check `py -m pip show enterprise-connectors` |
| `ModuleNotFoundError: No module named 'dotenv'` | `python-dotenv` not installed | `py -m pip install --user python-dotenv` |
| `ConfigurationError: Loading YAML requires PyYAML` | Installed without the `yaml` extra | Re-run Step 2 with `[postgres,yaml]` |
| `ConfigurationError: Optional dependency 'psycopg' is not installed` | Installed without the `postgres` extra | Re-run Step 2 with `[postgres,yaml]` |
| `ConfigurationError: Cannot read connections file ...` | Wrong path, or a YAML mistake (often a tab or wrong indentation) | Use `HERE / "connections.yaml"`; replace tabs with spaces |
| `... environment variable EC_POSTGRES_PASSWORD ... is not set` | `.env` wasn't loaded, or the name is misspelled | Check `load_dotenv(HERE / ".env")` runs first; compare names in `.env` and `connections.yaml` |
| `connection 'x' is invalid and was not loaded - ...` | That entry in `connections.yaml` is wrong; the message says what | Fix the named setting; the other connections still work meanwhile |
| `ConnectionTimeout` / `ConnectionError` on `localhost` | Wrong port, or PostgreSQL not running | `Test-NetConnection localhost -Port 5433` (and 5432); `Get-Service postgresql*` |
| `.\.venv\Scripts\Activate.ps1 cannot be loaded because running scripts is disabled` | PowerShell execution policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (only needed for virtual environments) |

**Is the right version installed?** `py -m pip show enterprise-connectors` shows `Version:` and `Location:`.

---

## Logging connections

Register one function with `manager.add_hook(...)`. The package calls it every time something happens to a
connection, and you send it to your normal logging:

```python
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app.db")

def log_db_event(event):
    a = event.attributes
    log.log(
        logging.INFO if event.success else logging.WARNING,
        "event=%s connection=%s connection_id=%s lease=%s ok=%s duration_ms=%s error=%s",
        event.name, a.get("connection_name"), a.get("connection_id"), a.get("lease_id"),
        event.success, event.duration_ms, event.error_type,
    )

manager = ConnectorManager.from_yaml(HERE / "connections.yaml")
manager.add_hook(log_db_event)
```

Example output:

```
INFO:app.db:event=connection.established connection=pipeline_db connection_id=53216 lease=None ok=True duration_ms=26.8 error=None
INFO:app.db:event=pool.checkout connection=pipeline_db connection_id=53216 lease=1 ok=True duration_ms=None error=None
INFO:app.db:event=pool.return connection=pipeline_db connection_id=53216 lease=1 ok=True duration_ms=None error=None
```

| Field | Meaning |
|---|---|
| `event.name` | What happened: `connection.established`, `connection.failed`, `connection.health_check`, `pool.checkout`, `pool.return`, `pool.recycled`, `pool.exhausted`, `pool.closed`, `auth.authenticated`, `auth.refreshed`, `connector.closed` |
| `connection_name` | The name from `connections.yaml` (e.g. `pipeline_db`) |
| `connection_id` | The database session ID (Postgres backend PID). It is the same number DBAs see in `pg_stat_activity.pid` |
| `lease_id` | One borrow of a connection. The `pool.checkout` and `pool.return` with the same `lease_id` belong together |
| `success`, `error_type` | Whether it worked, and the error class if not (e.g. `AuthenticationError`) |
| `duration_ms` | How long connecting / the health check took |
| `tenant_id`, `principal` | Tenant (if configured) and database username |
| `reason` | On `pool.recycled`: `expired`, `health_check_failed`, `discarded` or `shutdown` |

**Health check result:** `db.test_connection()` fills `db.last_result` with `ok`, `latency_ms`,
`error_type`, `message`, `connection_name` and `connection_id`.

**Errors:** catch `ConnectorError`. `str(error)`, `type(error).__name__`, `error.connector_type` and
`error.driver_error` are all safe to log.

**Startup problems:** log `manager.load_errors` once after loading.

**Inside your own code:** `conn.info.backend_pid` gives the same `connection_id` for the connection you
are using, so your query logs can be matched to the events.

Nothing here ever contains a password. The package's own log messages (logger `enterprise_connectors`)
are also secret-free and appear once your app configures logging.

---

## Adding another database

1. Add its values to `.env` with new variable names:

   ```dotenv
   REPORT_HOST=reports.mycompany.local
   REPORT_USER=report_reader
   REPORT_PASSWORD=...
   ```

2. Add a block to `connections.yaml` with a new name:

   ```yaml
     reporting_db:
       type: postgres
       config: {host: env://REPORT_HOST, port: 5432, database: reporting}
       credentials: {username: env://REPORT_USER, password: env://REPORT_PASSWORD}
   ```

3. Run `py check_connections.py`, then use it by name:

   ```python
   reporting = manager.get("reporting_db")
   ```

If one database's settings are wrong, the others still load. The bad one is listed in `manager.load_errors`.

---

## Upgrading, uninstalling and other database types

**Upgrade to a new version.** Unzip the new bundle (Step 1), then install the new wheel. It replaces the old one:

```powershell
py -m pip install --user "C:\packages\enterprise-connectors-0.2.0\enterprise_connectors-0.2.0-py3-none-any.whl[postgres,yaml]"
py -m pip show enterprise-connectors        # Version: 0.2.0
```

If you were given a rebuilt file with the **same** version number, add `--force-reinstall`.
Your `.env`, `connections.yaml` and code stay as they are. Check the new bundle's guide for anything new.

**Other database types.** New versions add more databases (for example MySQL or Snowflake). Each one has its
own extra, named after its type, so you install only what you use:

```powershell
py -m pip install --user "<new wheel>[postgres,mysql,yaml]"
```

Then use `type: mysql` (or whichever) in `connections.yaml`. If you forget the extra, the error message
tells you exactly what to install, and your other databases keep working.

**Uninstall:**

```powershell
py -m pip uninstall enterprise-connectors
```

---

## Command cheat sheet

| Task | Command |
|---|---|
| Check Python | `py --version` |
| Check pip | `py -m pip --version` |
| Check the database is reachable | `Test-NetConnection <host> -Port <port>` |
| Unzip the bundle | `Expand-Archive -Path <zip> -DestinationPath C:\packages\enterprise-connectors-0.1.0` |
| Install | `py -m pip install --user "<folder>\enterprise_connectors-0.1.0-py3-none-any.whl[postgres,yaml]"` |
| Install the .env reader | `py -m pip install --user python-dotenv` |
| Check the installed version | `py -m pip show enterprise-connectors` |
| Create `.env` | `Copy-Item <folder>\.env.example .env` then `notepad .env` |
| Create `connections.yaml` | `Copy-Item <folder>\connections.example.yaml connections.yaml` |
| Keep `.env` out of git | `if (-not (Test-Path .gitignore) -or -not (Select-String -Path .gitignore -Pattern '^\.env$' -Quiet)) { Add-Content .gitignore ".env" }` |
| Test all connections | `py check_connections.py` |
| Run your program | `py app.py` |
| Upgrade | install the new `.whl` with the same command |
| Uninstall | `py -m pip uninstall enterprise-connectors` |

## Need help?

Contact the connector team (the maintainers of `enterprise-connectors`) with:
- the output of `py check_connections.py`
- the output of `py -m pip show enterprise-connectors`
- the full error message

None of these contain passwords, so they are safe to share. Never send your `.env` file.
