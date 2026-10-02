#!/usr/bin/env python
"""Connector connection test harness.

Loads connectors_test.yaml, passes each test's connection info into the REAL connector
code, checks that a live connection object comes back, and writes an HTML report, a JSON
results file and a JSON file of connection-object details to test_reports/.

Dependencies: standard library + PyYAML only.

    py connector_tests\\run_connector_tests.py --env-file .env
"""

from __future__ import annotations

import argparse
import datetime as dt
import enum
import importlib
import json
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "connectors_test.yaml"
DEFAULT_REPORT_DIR = HERE / "test_reports"
TEMPLATE_FILE = HERE / "report_template.html"

MASK = "********"
SENSITIVE_KEY = re.compile(r"password|secret|token|key|credential|pwd|auth", re.IGNORECASE)
REFERENCE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<body>.*)$", re.IGNORECASE)
SUPPORTED_SCHEMES = {"env"}
# Secret-store schemes we recognise but cannot resolve. Other schemes (https://, s3://, ...) are literal values.
UNSUPPORTED_SECRET_SCHEMES = {
    "vault",
    "aws-sm",
    "awssm",
    "ssm",
    "secretsmanager",
    "azure-kv",
    "akv",
    "keyvault",
    "gcp-sm",
    "gsm",
    "file",
    "secret",
    "keyring",
    "op",
}
CALL_STYLES = {"type_and_config", "dict", "kwargs", "type_config_credentials", "named"}

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Category(str, enum.Enum):
    OK = "ok"
    EXPECTED_FAILURE = "expected failure"
    SKIPPED = "skipped"
    CONFIG = "invalid test definition"
    SECRET_NOT_RESOLVED = "secret not resolved"
    IMPORT_ERROR = "import error"
    EXCEPTION = "exception"
    TIMEOUT = "timeout"
    RETURNED_NONE = "returned None"
    TYPE_MISMATCH = "type mismatch"
    HEALTH_CHECK_FAILED = "health check failed"
    POOL_CHECK_FAILED = "pool check failed"
    UNEXPECTED_SUCCESS = "unexpected success"
    ERROR_MISMATCH = "wrong error"


# Categories caused by the test setup rather than the connector; a negative test never passes on these
# unless it explicitly expects that category.
SETUP_CATEGORIES = {Category.CONFIG, Category.SECRET_NOT_RESOLVED, Category.IMPORT_ERROR}


class HarnessError(Exception):
    """Invalid YAML / test definition."""


class SecretNotResolved(Exception):
    pass


# --------------------------------------------------------------------------------------------
# Secrets: resolution, display masking and scrubbing
# --------------------------------------------------------------------------------------------


def load_env_file(path: str | Path | None) -> dict[str, str]:
    """Parse KEY=VALUE lines. Values are kept in memory only (never exported to os.environ)."""
    if not path:
        return {}
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def _is_sensitive(path: tuple[str, ...]) -> bool:
    """True when the value's own key (the last non-index path segment) names a secret."""
    keys = [p for p in path if not p.isdigit()]
    return bool(keys) and bool(SENSITIVE_KEY.search(keys[-1]))


class Secrets:
    """Resolves env:// references and remembers every secret value so it can be scrubbed from output."""

    def __init__(self, env_file: dict[str, str], public: set[str] | None = None) -> None:
        self.env_file = env_file
        self.values: set[str] = set()
        # Words already printed as test names/types. A "secret" equal to one of them is not hidden as free text
        # (that would only blank out the connector's name); key- and pattern-based masking still applies.
        self.public = public or set()

    def lookup(self, name: str) -> str | None:
        return self.env_file.get(name, os.environ.get(name))

    def resolve(self, value: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            return {k: self.resolve(v, path + (str(k),)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, path + (str(i),)) for i, v in enumerate(value)]
        if isinstance(value, str):
            match = REFERENCE.match(value)
            scheme = match.group("scheme").lower() if match else None
            if scheme in SUPPORTED_SCHEMES:
                return self._resolve_env(match.group("body"), path)  # type: ignore[union-attr]
            if scheme in UNSUPPORTED_SECRET_SCHEMES:
                raise SecretNotResolved(
                    f"unsupported secret scheme '{scheme}://' (only env:// is supported) [at {'.'.join(path)}]"
                )
        if _is_sensitive(path) and value not in (None, ""):
            self.values.add(str(value))
        return value

    def _resolve_env(self, body: str, path: tuple[str, ...]) -> Any:
        name, _, query = body.partition("?")
        options = {k: v[-1] for k, v in parse_qs(query).items()}
        raw = self.lookup(name)
        if raw in (None, ""):
            if "default" not in options:
                raise SecretNotResolved(
                    f"env://{name} is not set (checked --env-file and environment) [at {'.'.join(path)}]"
                )
            raw = options["default"]
        if _is_sensitive(path) or SENSITIVE_KEY.search(name):
            self.values.add(raw)  # type: ignore[arg-type]
        kind = options.get("type", "str")
        try:
            if kind == "int":
                return int(raw)  # type: ignore[arg-type]
            if kind == "float":
                return float(raw)  # type: ignore[arg-type]
            if kind == "bool":
                return str(raw).strip().lower() in {"1", "true", "yes", "on"}
        except ValueError:
            raise SecretNotResolved(f"env://{name} is not a valid {kind} [at {'.'.join(path)}]") from None
        return raw

    def scrub(self, text: str) -> str:
        """Remove every known secret value plus common secret patterns from free text."""
        for secret in sorted(self.values, key=len, reverse=True):
            if len(secret) >= 3 and secret not in self.public:
                text = text.replace(secret, MASK)
        text = re.sub(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", rf"\1 {MASK}", text)
        text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s]+@", rf"\1{MASK}@", text)
        return re.sub(
            r"""(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|credential)["']?\s*[:=]\s*["']?)[^\s"'&,;}]+""",
            rf"\1{MASK}",
            text,
        )


def display_value(value: Any, path: tuple[str, ...] = ()) -> Any:
    """Connection info for display: env:// references shown as references, literal secrets masked."""
    if isinstance(value, dict):
        return {k: display_value(v, path + (str(k),)) for k, v in value.items()}
    if isinstance(value, list):
        return [display_value(v, path + (str(i),)) for i, v in enumerate(value)]
    if isinstance(value, str) and REFERENCE.match(value):
        return value
    if _is_sensitive(path) and value not in (None, ""):
        return MASK
    return value


# --------------------------------------------------------------------------------------------
# Object inspection (feeds the integration layer)
# --------------------------------------------------------------------------------------------


def full_type_name(obj: Any) -> str:
    cls = obj if isinstance(obj, type) else type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _safe_repr(obj: Any, limit: int = 2000) -> str:
    try:
        text = repr(obj)
    except Exception as exc:
        text = f"<repr failed: {type(exc).__name__}>"
    return text if len(text) <= limit else text[:limit] + "…"


def to_jsonable(value: Any, secrets: Secrets, depth: int) -> Any:
    if isinstance(value, enum.Enum):  # before int: IntEnum values would otherwise lose their names
        return f"{type(value).__name__}.{value.name}"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return secrets.scrub(value)[:2000]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {
            str(k): MASK if SENSITIVE_KEY.search(str(k)) and v not in (None, "") else to_jsonable(v, secrets, depth - 1)
            for k, v in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v, secrets, depth - 1) for v in list(value)[:50]]
    if depth > 0:
        return {"type": full_type_name(value), "attributes": public_attributes(value, secrets, depth)}
    return secrets.scrub(_safe_repr(value, 500))


def public_attributes(obj: Any, secrets: Secrets, depth: int) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for name in sorted(dir(obj)):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception as exc:
            attributes[name] = f"<unavailable: {type(exc).__name__}>"
            continue
        if callable(value):
            continue
        if SENSITIVE_KEY.search(name):
            attributes[name] = MASK if value not in (None, "", b"") else value
            continue
        attributes[name] = to_jsonable(value, secrets, depth - 1)
    return attributes


def describe_object(obj: Any, secrets: Secrets, depth: int) -> dict[str, Any]:
    return {
        "type": full_type_name(obj),
        "repr": secrets.scrub(_safe_repr(obj)),
        "attributes": public_attributes(obj, secrets, depth),
    }


# --------------------------------------------------------------------------------------------
# Factory resolution
# --------------------------------------------------------------------------------------------


@dataclass
class FactoryRef:
    """``module:attr.path`` where a segment ending in ``()`` is instantiated with no arguments per test,
    e.g. ``enterprise_connectors:ConnectorManager().create``."""

    spec: str
    module: ModuleType
    segments: list[tuple[str, bool]]

    @property
    def kind(self) -> str:
        target: Any = self.module
        for name, called in self.segments:
            target = getattr(target, name)
            if called:
                return "method of instance"
        return "class" if isinstance(target, type) else "function"

    def build(self, owners: list[Any]) -> Callable[..., Any]:
        target: Any = self.module
        for name, called in self.segments:
            target = getattr(target, name)
            if called:
                target = target()
                owners.append(target)
        if not callable(target):
            raise TypeError(f"{self.spec} is not callable")
        return target  # type: ignore[no-any-return]


def import_object(spec: str) -> tuple[ModuleType, list[tuple[str, bool]]]:
    if ":" not in spec:
        raise HarnessError(f"'{spec}' must look like module:callable")
    module_name, _, path = spec.partition(":")
    module = importlib.import_module(module_name)
    segments = []
    target: Any = module
    for part in path.split("."):
        called = part.endswith("()")
        name = part[:-2] if called else part
        if not name.isidentifier():
            raise HarnessError(f"'{spec}': invalid attribute '{part}'")
        if not hasattr(target, name):
            raise AttributeError(
                f"{spec}: '{name}' not found on {full_type_name(target) if not isinstance(target, ModuleType) else target.__name__}"
            )
        target = getattr(target, name)
        segments.append((name, called))
    return module, segments


def resolve_factory(spec: str) -> FactoryRef:
    module, segments = import_object(spec)
    return FactoryRef(spec, module, segments)


def load_class(spec: str) -> Any:
    module, segments = import_object(spec)
    target: Any = module
    for name, _ in segments:
        target = getattr(target, name)
    return target


def type_matches(obj: Any, expected: str) -> bool:
    if ":" in expected:
        try:
            return isinstance(obj, load_class(expected))
        except Exception:
            return False
    names = {c.__qualname__ for c in type(obj).__mro__} | {full_type_name(c) for c in type(obj).__mro__}
    return expected in names


# --------------------------------------------------------------------------------------------
# Running a test
# --------------------------------------------------------------------------------------------


@dataclass
class Suite:
    path: Path
    defaults: dict[str, Any]
    tests: list[dict[str, Any]]
    env_file: dict[str, str] = field(default_factory=dict)

    @property
    def public_words(self) -> set[str]:
        """Connector types and the words of test names: authored in the YAML, shown on the main tab."""
        words: set[str] = set()
        for test in self.tests:
            if isinstance(test, dict):
                if test.get("type"):
                    words.add(str(test["type"]))
                words.update(w for w in re.split(r"[^A-Za-z0-9]+", str(test.get("name", ""))) if w)
        return words


@dataclass
class TestResult:
    name: str
    type: str | None
    status: str
    category: str
    reason: str
    duration_s: float
    expect: dict[str, Any]
    connection_info: dict[str, Any]
    factory: dict[str, Any]
    connection_object: dict[str, Any] | None = None
    health_check: dict[str, Any] | None = None
    traceback: str | None = None
    pool: dict[str, Any] | None = None
    concurrency: dict[str, Any] | None = None
    events: list[dict[str, Any]] | None = None


def _connections_file_tests(spec: Any, base: Path) -> list[dict[str, Any]]:
    """One test per entry of a connections file. An unreadable file becomes a single failing test."""
    if isinstance(spec, str):
        spec = {"path": spec}
    file = (base / str(spec["path"])).resolve()
    common = {
        "call_style": "named",
        "factory": spec.get("factory", "enterprise_connectors:ConnectorManager.from_yaml"),
        "connections_path": str(file),
        **(spec.get("overrides") or {}),
    }
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        entries = data.get("connections") if isinstance(data, dict) else None
        if not isinstance(entries, dict) or not entries:
            raise ValueError("it has no non-empty 'connections' mapping")
    except (OSError, yaml.YAMLError, ValueError) as exc:
        return [{"name": file.name, "load_error": f"Cannot read connections file {file}: {exc}", "overrides": common}]
    per_name = spec.get("tests") or {}
    by_type = spec.get("expect_by_type") or {}
    tests = []
    for name, entry in entries.items():
        entry = entry if isinstance(entry, dict) else {}
        extra = per_name.get(name) or {}
        ctype = entry.get("type")
        tests.append(
            {
                "name": str(name),
                "type": ctype,
                "enabled": extra.get("enabled", True),
                "config": entry.get("config") or {},
                "credentials": entry.get("credentials") or {},
                "overrides": {**common, **(extra.get("overrides") or {})},
                "expect": {**(by_type.get(ctype) or {}), **(extra.get("expect") or {})},
            }
        )
    return tests


def load_suite(config: str | Path = DEFAULT_CONFIG, env_file: str | Path | None = None) -> Suite:
    path = Path(config).resolve()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HarnessError(f"Cannot read {path}: {exc}") from None
    defaults = data.get("defaults") or {}
    tests = data.get("tests") or []
    if not isinstance(defaults, dict) or not isinstance(tests, list):
        raise HarnessError("'defaults' must be a mapping and 'tests' a list")
    if data.get("connections_file"):
        tests = tests + _connections_file_tests(data["connections_file"], path.parent)
    names = [t.get("name") for t in tests if isinstance(t, dict)]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise HarnessError(f"Duplicate test names: {', '.join(sorted(map(str, duplicates)))}")
    for entry in defaults.get("python_path") or []:
        resolved = str((path.parent / entry).resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
    return Suite(path=path, defaults=defaults, tests=tests, env_file=load_env_file(env_file))


def _settings(test: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    merged = {k: v for k, v in defaults.items() if k != "python_path"}
    merged.update(test.get("overrides") or {})
    return merged


def _call_factory(fn: Callable[..., Any], style: str, ctype: str | None, config: dict[str, Any], creds: Any) -> Any:
    if style == "type_config_credentials":
        return fn(ctype, config, creds)
    merged = dict(config)
    if isinstance(creds, dict):
        merged.update(creds)
    if style == "type_and_config":
        return fn(ctype, merged)
    if style == "dict":
        if ctype is not None and "type" not in merged:
            merged["type"] = ctype
        return fn(merged)
    return fn(**merged)  # kwargs


def _run_health_check(conn: Any, spec: Any) -> dict[str, Any]:
    if isinstance(spec, str):
        method, args, kwargs = spec, [], {}
    else:
        method, args, kwargs = spec.get("method"), spec.get("args") or [], spec.get("kwargs") or {}
    call = f"{method}({', '.join([repr(a) for a in args] + [f'{k}={v!r}' for k, v in kwargs.items()])})"
    result = getattr(conn, method)(*args, **kwargs)
    return {"call": call, "ok": result is not False, "returned": full_type_name(result) if result is not None else None}


def _resolve_target(handle: Any, owners: list[Any], path: str) -> Any:
    """``handle.a.b`` starts at the factory's result, ``owner.a.b`` at the first object created by ``()``."""
    root, _, rest = path.partition(".")
    if root == "handle":
        target = handle
    elif root == "owner" and owners:
        target = owners[0]
    else:
        raise HarnessError(f"pool_probe path '{path}' must start with 'handle.' or 'owner.'")
    for part in filter(None, rest.split(".")):
        target = getattr(target, part)
    return target


def _run_pool_probe(
    handle: Any, owners: list[Any], probe: dict[str, Any], health_spec: Any, secrets: Secrets
) -> dict[str, Any]:
    """Check out pooled connections ``checkouts`` times, recording pool stats during and after each one."""
    method = str(probe.get("context_method", "connection"))
    stats_fn = _resolve_target(handle, owners, str(probe["stats"])) if probe.get("stats") else None

    def snapshot() -> Any:
        if stats_fn is None:
            return None
        stats = stats_fn()
        items = stats.items() if isinstance(stats, dict) else [(None, stats)]
        return to_jsonable([{"identity": k, "stats": v} for k, v in items], secrets, 6)

    info: dict[str, Any] = {"context_method": method, "checkouts": []}
    first_id = None
    for n in range(1, int(probe.get("checkouts", 2)) + 1):
        with getattr(handle, method)() as pooled:
            first_id = id(pooled) if first_id is None else first_id
            entry: dict[str, Any] = {
                "n": n,
                "object_type": full_type_name(pooled),
                "same_as_first": id(pooled) == first_id,
            }
            if health_spec:
                entry["health_check"] = _run_health_check(pooled, health_spec)
            entry["stats_during"] = snapshot()
        entry["stats_after_return"] = snapshot()
        info["checkouts"].append(entry)
    info["reused"] = all(c["same_as_first"] for c in info["checkouts"][1:]) if len(info["checkouts"]) > 1 else None
    return info


def _call_spec(target: Any, spec: dict[str, Any]) -> Any:
    """Call ``target.method(*args, **kwargs)``, then optionally ``.then()`` on the result."""
    result = getattr(target, spec["method"])(*(spec.get("args") or []), **(spec.get("kwargs") or {}))
    if spec.get("then"):
        result = getattr(result, spec["then"])()
    return result


def _pool_in_use(stats_fn: Callable[[], Any] | None) -> int | None:
    if stats_fn is None:
        return None
    stats = stats_fn()
    values = stats.values() if isinstance(stats, dict) else [stats]
    return sum(int(getattr(v, "in_use", 0)) for v in values)


def _run_concurrency_probe(handle: Any, owners: list[Any], probe: dict[str, Any], secrets: Secrets) -> dict[str, Any]:
    """Simulate ``users`` callers asking for a pooled connection at the same moment.

    Each user holds its connection for ``hold_seconds`` and runs ``identify`` inside it (e.g. to learn which
    server session served it), so the report can show how many real connections served how many users.
    """
    users = int(probe.get("users", 10))
    hold = float(probe.get("hold_seconds", 0.2))
    method = str(probe.get("context_method", "connection"))
    columns = list(probe.get("columns") or [])
    key = str(probe.get("connection_key", ""))
    stats_fn = _resolve_target(handle, owners, str(probe["stats"])) if probe.get("stats") else None
    gate = threading.Barrier(users)
    origin = time.perf_counter()
    rows: list[dict[str, Any]] = [{} for _ in range(users)]

    def user(i: int) -> None:
        row: dict[str, Any] = {"user": i + 1}
        try:
            gate.wait(timeout=10)
            t0 = time.perf_counter()
            with getattr(handle, method)() as conn:
                t1 = time.perf_counter()
                row["in_use_at_checkout"] = _pool_in_use(stats_fn)
                if probe.get("identify"):
                    ident = _call_spec(conn, probe["identify"])
                    if columns and isinstance(ident, (tuple, list)):
                        row.update({c: to_jsonable(v, secrets, 1) for c, v in zip(columns, ident, strict=False)})
                    else:
                        row["identity"] = to_jsonable(ident, secrets, 1)
                row["connection_object_id"] = id(conn)
                time.sleep(hold)
                t2 = time.perf_counter()
            row.update(
                start_ms=round((t0 - origin) * 1000, 1),
                wait_ms=round((t1 - t0) * 1000, 1),
                hold_ms=round((t2 - t1) * 1000, 1),
            )
        except Exception as exc:
            row["error"] = secrets.scrub(f"{type(exc).__name__}: {exc}")
        rows[i] = row

    threads = [threading.Thread(target=user, args=(i,), daemon=True, name=f"probe-user-{i + 1}") for i in range(users)]
    for t in threads:
        t.start()
    deadline = time.perf_counter() + float(probe.get("timeout_seconds", 60))
    for t in threads:
        t.join(max(0.0, deadline - time.perf_counter()))
    for i, t in enumerate(threads):
        if t.is_alive():
            rows[i] = {"user": i + 1, "error": "still waiting for a connection when the probe timed out"}

    served = [r for r in rows if "error" not in r]
    ident_of = (
        (lambda r: r.get(key)) if key and any(key in r for r in served) else (lambda r: r["connection_object_id"])
    )
    by_connection: dict[str, list[int]] = {}
    for r in served:
        by_connection.setdefault(str(ident_of(r)), []).append(r["user"])
    in_use = [r["in_use_at_checkout"] for r in served if r.get("in_use_at_checkout") is not None]
    sessions = [r["server_sessions"] for r in served if isinstance(r.get("server_sessions"), int)]
    max_size = None
    if stats_fn is not None:
        stats = stats_fn()
        for k in stats if isinstance(stats, dict) else []:
            max_size = getattr(getattr(k, "pool_config", None), "max_size", max_size)
    for r in rows:
        r.pop("connection_object_id", None)
    return {
        "users": users,
        "served": len(served),
        "failed": users - len(served),
        "hold_seconds": hold,
        "pool_max_size": max_size,
        "distinct_connections": len(by_connection),
        "connection_key": key if key and any(key in r for r in served) else "python object id",
        "users_by_connection": by_connection,
        "peak_in_use": max(in_use) if in_use else None,
        "max_wait_ms": max((r["wait_ms"] for r in served), default=None),
        "max_server_sessions": max(sessions) if sessions else None,
        "total_ms": round((time.perf_counter() - origin) * 1000, 1),
        "rows": rows,
    }


def _close(target: Any, method: str | None) -> None:
    if target is None or not method:
        return
    try:
        getattr(target, method)()
    except Exception:  # noqa: S110 - cleanup errors are deliberately ignored
        pass


def run_test(test: dict[str, Any], suite: Suite) -> TestResult:
    start = time.perf_counter()
    name = str(test.get("name") or "<unnamed>")
    ctype = test.get("type")
    settings = _settings(test, suite.defaults)
    expect = {"success": True, **(test.get("expect") or {})}
    connection_info = {
        "config": display_value(test.get("config") or {}),
        "credentials": display_value(test.get("credentials") or {}, ("credentials",)),
    }
    factory_info: dict[str, Any] = {
        "factory": settings.get("factory"),
        "call_style": settings.get("call_style"),
        "connect_method": settings.get("connect_method"),
        "health_check": settings.get("health_check"),
        "close_method": settings.get("close_method"),
        "timeout_seconds": settings.get("timeout_seconds", 30),
    }
    secrets = Secrets(suite.env_file, suite.public_words)

    def finish(ok: bool, category: Category, reason: str, **extra: Any) -> TestResult:
        status, category, reason = _judge(ok, category, reason, expect)
        return TestResult(
            name=name,
            type=ctype,
            status=status,
            category=category.value,
            reason=secrets.scrub(reason),
            duration_s=round(time.perf_counter() - start, 3),
            expect=expect,
            connection_info=connection_info,
            factory=factory_info,
            **extra,
        )

    if not test.get("enabled", True):
        return TestResult(
            name, ctype, SKIP, Category.SKIPPED.value, "disabled in YAML", 0.0, expect, connection_info, factory_info
        )

    if test.get("load_error"):
        return finish(False, Category.CONFIG, str(test["load_error"]))

    style = settings.get("call_style", "type_and_config")
    if style == "named":
        factory_info["connections_file"] = settings.get("connections_path")
    if style not in CALL_STYLES:
        return finish(
            False, Category.CONFIG, f"unknown call_style '{style}' (use one of {', '.join(sorted(CALL_STYLES))})"
        )
    if not settings.get("factory"):
        return finish(False, Category.CONFIG, "no factory configured")
    try:
        timeout = float(settings.get("timeout_seconds", 30))
    except (TypeError, ValueError):
        return finish(False, Category.CONFIG, "timeout_seconds must be a number")

    try:
        config = secrets.resolve(test.get("config") or {}, ())
        raw_creds = secrets.resolve(test.get("credentials") or {}, ("credentials",))
    except SecretNotResolved as exc:
        if style != "named":
            return finish(False, Category.SECRET_NOT_RESOLVED, str(exc))
        config, raw_creds = {}, {}  # the SDK resolves (and reports) references for named connections

    try:
        factory = resolve_factory(str(settings["factory"]))
        factory_info["factory_kind"] = factory.kind
        creds: Any = raw_creds
        if style == "type_config_credentials":
            cred_class = settings.get("credentials_class")
            creds = load_class(cred_class)(**raw_creds) if cred_class else raw_creds
    except HarnessError as exc:
        return finish(False, Category.CONFIG, str(exc))
    except Exception as exc:
        return finish(
            False,
            Category.IMPORT_ERROR,
            f"{type(exc).__name__}: {exc}",
            traceback=secrets.scrub(traceback.format_exc()),
        )

    state: dict[str, Any] = {"stage": "factory"}
    events: list[dict[str, Any]] = []
    depth = int(settings.get("capture_depth", 2))

    def worker() -> None:
        owners: list[Any] = []
        handle = conn = None
        try:
            fn = factory.build(owners)
            if style == "named":  # load the connections file the way an application would
                owners.append(fn(settings["connections_path"], environ={**os.environ, **suite.env_file}))
            if settings.get("event_hook"):
                _resolve_target(None, owners, str(settings["event_hook"]))(
                    lambda event: events.append(
                        {
                            "name": getattr(event, "name", str(event)),
                            "success": getattr(event, "success", None),
                            "error_type": getattr(event, "error_type", None),
                            "duration_ms": getattr(event, "duration_ms", None),
                            "attributes": dict(getattr(event, "attributes", None) or {}),
                            "at_ms": round((time.perf_counter() - start) * 1000, 1),
                        }
                    )
                )
            if style == "named":
                handle = owners[-1].get(name)
            else:
                handle = _call_factory(fn, style, ctype, config, creds)
            state["stage"] = "connect"
            method = settings.get("connect_method")
            conn = getattr(handle, method)() if method else handle
            state["returned"] = conn is not None
            if conn is not None:
                state["type_name"] = full_type_name(conn)
                state["type_ok"] = type_matches(conn, expect["object_type"]) if expect.get("object_type") else True
                state["object"] = describe_object(conn, secrets, depth)
                if settings.get("health_check"):
                    state["stage"] = "health_check"
                    try:
                        state["health"] = _run_health_check(conn, settings["health_check"])
                    except Exception as exc:
                        state["health"] = {
                            "call": str(settings["health_check"]),
                            "ok": False,
                            "error": secrets.scrub(f"{type(exc).__name__}: {exc}"),
                        }
                        state["health_tb"] = secrets.scrub(traceback.format_exc())
                if conn is not handle:
                    _close(conn, settings.get("close_method"))  # raw test connection is done; probes use the pool
                if settings.get("pool_probe"):
                    state["stage"] = "pool_probe"
                    try:
                        state["pool"] = _run_pool_probe(
                            handle, owners, settings["pool_probe"], settings.get("health_check"), secrets
                        )
                    except Exception as exc:
                        state["pool"] = {"error": secrets.scrub(f"{type(exc).__name__}: {exc}")}
                        state["pool_tb"] = secrets.scrub(traceback.format_exc())
                if settings.get("concurrency_probe"):
                    state["stage"] = "concurrency_probe"
                    try:
                        state["concurrency"] = _run_concurrency_probe(
                            handle, owners, settings["concurrency_probe"], secrets
                        )
                    except Exception as exc:
                        state["concurrency"] = {"error": secrets.scrub(f"{type(exc).__name__}: {exc}")}
                        state["pool_tb"] = secrets.scrub(traceback.format_exc())
        except BaseException as exc:
            state["error"] = secrets.scrub(f"{type(exc).__name__}: {exc}")
            state["traceback"] = secrets.scrub(traceback.format_exc())
        finally:
            if conn is not None and conn is not handle:
                _close(conn, settings.get("close_method"))
            _close(handle, settings.get("handle_close_method", settings.get("close_method")))
            for owner in reversed(owners):
                _close(owner, "close")
            state["done"] = True

    thread = threading.Thread(target=worker, name=f"connector-test:{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _ABANDONED.append(thread)
        return finish(
            False,
            Category.TIMEOUT,
            f"no result after {timeout:g}s during {state['stage']}; attempt abandoned "
            "(daemon thread closes its resources if it ever finishes)",
        )

    extra = {
        "connection_object": state.get("object"),
        "health_check": state.get("health"),
        "traceback": state.get("traceback") or state.get("health_tb") or state.get("pool_tb"),
        "pool": state.get("pool"),
        "concurrency": state.get("concurrency"),
        "events": list(events) if settings.get("event_hook") else None,
    }
    if "error" in state:
        return finish(False, Category.EXCEPTION, f"{state['stage']} raised {state['error']}", **extra)
    if not state.get("returned"):
        return finish(
            False, Category.RETURNED_NONE, f"{settings.get('connect_method') or 'factory'}() returned None", **extra
        )
    if not state.get("type_ok"):
        return finish(
            False, Category.TYPE_MISMATCH, f"got {state['type_name']}, expected {expect['object_type']}", **extra
        )
    health = state.get("health")
    if health is not None and not health["ok"]:
        detail = health.get("error") or "returned False"
        return finish(False, Category.HEALTH_CHECK_FAILED, f"{health['call']} {detail}", **extra)
    pool = state.get("pool") or {}
    if pool.get("error"):
        return finish(False, Category.POOL_CHECK_FAILED, f"pooled checkout raised {pool['error']}", **extra)
    failed_checkout = next(
        (c for c in pool.get("checkouts", []) if not c.get("health_check", {"ok": True})["ok"]), None
    )
    if failed_checkout:
        return finish(
            False,
            Category.POOL_CHECK_FAILED,
            f"health check failed on pooled checkout #{failed_checkout['n']}",
            **extra,
        )
    probe = state.get("concurrency") or {}
    if probe.get("error"):
        return finish(False, Category.POOL_CHECK_FAILED, f"concurrent users probe raised {probe['error']}", **extra)
    if probe.get("failed"):
        return finish(
            False,
            Category.POOL_CHECK_FAILED,
            f"{probe['failed']} of {probe['users']} concurrent users got no connection",
            **extra,
        )
    return finish(True, Category.OK, f"connected: {state['type_name']}", **extra)


_ABANDONED: list[threading.Thread] = []


def _judge(ok: bool, category: Category, reason: str, expect: dict[str, Any]) -> tuple[str, Category, str]:
    """Apply the test's expectation. Returns (status, category, reason)."""
    if expect.get("success", True):
        return (PASS if ok else FAIL), category, reason
    if ok:
        return FAIL, Category.UNEXPECTED_SUCCESS, f"expected a failure but {reason}"
    wanted_category = expect.get("category")
    if wanted_category and category.value != str(wanted_category).replace("_", " "):
        return FAIL, Category.ERROR_MISMATCH, f"expected category '{wanted_category}', got '{category.value}': {reason}"
    if not wanted_category and category in SETUP_CATEGORIES:
        return FAIL, category, reason
    needles = expect.get("error_contains")
    if isinstance(needles, str):
        needles = [needles]
    if needles and not any(str(n).lower() in reason.lower() for n in needles):
        wanted = " or ".join(repr(n) for n in needles)
        return FAIL, Category.ERROR_MISMATCH, f"error did not contain {wanted}: {reason}"
    return PASS, Category.EXPECTED_FAILURE, reason


# --------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------


def run_suite(suite: Suite, only: str | None = None) -> list[TestResult]:
    results = []
    for test in suite.tests:
        if not isinstance(test, dict):
            continue
        if only and only.lower() not in str(test.get("name", "")).lower():
            continue
        results.append(run_test(test, suite))
    return results


def summarize(results: list[TestResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(r.status == PASS for r in results),
        "failed": sum(r.status == FAIL for r in results),
        "skipped": sum(r.status == SKIP for r in results),
    }


def _final_scrub(text: str, suite: Suite) -> str:
    """Defence in depth: remove any secret value from the env file or YAML literals from the final output."""
    secrets = Secrets(suite.env_file, suite.public_words)
    for key, value in suite.env_file.items():
        if SENSITIVE_KEY.search(key) and value:
            secrets.values.add(value)
    for test in suite.tests:
        if isinstance(test, dict):
            try:
                secrets.resolve(test.get("config") or {}, ())
                secrets.resolve(test.get("credentials") or {}, ("credentials",))
            except SecretNotResolved:
                pass
    return secrets.scrub(text)


def load_flows(suite: Suite, types: set[str | None]) -> dict[str, Any]:
    """Flowcharts from ``defaults.flows_file`` (default flows.yaml next to the test YAML). Optional:
    a missing or invalid file only drops the flowchart tab content, never the run."""
    path = suite.path.parent / str(suite.defaults.get("flows_file", "flows.yaml"))
    if not path.is_file():
        return {}
    try:
        flows = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"warning: flow charts skipped, cannot read {path.name}: {exc}", file=sys.stderr)
        return {}
    return {t: flows[t] for t in sorted(str(t) for t in types if t) if isinstance(flows.get(t), dict)}


def write_reports(results: list[TestResult], suite: Suite, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    generated = dt.datetime.now().isoformat(timespec="seconds")
    rows = [{k: v for k, v in asdict(r).items()} for r in results]
    payload = {"generated_at": generated, "config": str(suite.path), "summary": summarize(results), "results": rows}
    objects = {
        "generated_at": generated,
        "connections": [
            {"name": r.name, "type": r.type, "status": r.status, "object": r.connection_object}
            for r in results
            if r.connection_object is not None
        ],
    }
    paths = {
        "html": out_dir / f"connector_report_{stamp}.html",
        "json": out_dir / f"connector_results_{stamp}.json",
        "objects": out_dir / f"connection_objects_{stamp}.json",
    }
    paths["json"].write_text(_final_scrub(json.dumps(payload, indent=2, default=str), suite), encoding="utf-8")
    paths["objects"].write_text(_final_scrub(json.dumps(objects, indent=2, default=str), suite), encoding="utf-8")
    html_payload = {**payload, "flows": load_flows(suite, {r.type for r in results})}
    data = _final_scrub(json.dumps(html_payload, default=str), suite).replace("</", "<\\/")
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    paths["html"].write_text(template.replace("__DATA__", data), encoding="utf-8")
    return paths


def print_results(results: list[TestResult]) -> None:
    width = max([len(r.name) for r in results] + [10])
    for r in results:
        detail = (
            r.reason if r.status != PASS or r.category != Category.OK.value else r.reason.removeprefix("connected: ")
        )
        tag = f"[{r.category}] " if r.status == FAIL else ""
        print(f"{r.status:<4}  {r.name:<{width}}  {r.duration_s:>7.2f}s  {tag}{detail}")
    s = summarize(results)
    print(f"\n{s['total']} tests: {s['passed']} passed, {s['failed']} failed, {s['skipped']} skipped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run connector connection tests from a YAML file.")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="YAML test file")
    parser.add_argument("--env-file", help="KEY=VALUE file used to resolve env:// references")
    parser.add_argument("--only", help="run only tests whose name contains this text")
    parser.add_argument("--out", default=str(DEFAULT_REPORT_DIR), help="report directory")
    parser.add_argument("--no-open", action="store_true", help="do not open the HTML report")
    args = parser.parse_args(argv)

    try:
        suite = load_suite(args.config, args.env_file)
    except (HarnessError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    results = run_suite(suite, args.only)
    if not results:
        print("No tests matched.", file=sys.stderr)
        return 1
    print_results(results)
    paths = write_reports(results, suite, Path(args.out))
    print(f"Report:  {paths['html']}\nResults: {paths['json']}\nObjects: {paths['objects']}")
    if not args.no_open:
        webbrowser.open(paths["html"].resolve().as_uri())
    return 0 if all(r.status != FAIL for r in results) else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    if _ABANDONED and any(t.is_alive() for t in _ABANDONED):
        os._exit(code)  # a hung connector thread must not delay process exit
    sys.exit(code)
