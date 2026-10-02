"""Health checking and stale-resource recycling."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..core.connection_result import ConnectionResult
from ..core.exceptions import ConnectorError, map_driver_error
from ..utils.logging import get_logger

_log = get_logger(__name__)


def run_check(check: Callable[[], None], connector_type: str) -> ConnectionResult:
    """Run ``check`` and return a timed ConnectionResult. Errors are mapped, never raised."""
    start = time.perf_counter()
    try:
        check()
    except ConnectorError as exc:
        return ConnectionResult.failure(connector_type, (time.perf_counter() - start) * 1000, exc)
    except Exception as exc:
        mapped = map_driver_error(exc, connector_type)
        return ConnectionResult.failure(connector_type, (time.perf_counter() - start) * 1000, mapped)
    return ConnectionResult.success(connector_type, (time.perf_counter() - start) * 1000)


class PoolMaintainer:
    """Background daemon thread that calls ``task`` (e.g. PoolManager.prune_all) every ``interval`` seconds."""

    def __init__(self, task: Callable[[], None], interval: float) -> None:
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self._task = task
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="enterprise-connectors-pool-maintainer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._task()
            except Exception:
                _log.warning("Pool maintenance failed", exc_info=True)
