"""PoolConfig: sizes, timeouts, lifetime, idle, health-check interval, exhaustion behaviour."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..core.exceptions import ConfigurationError


class ExhaustionPolicy(str, Enum):
    WAIT = "wait"  # block up to checkout_timeout, then PoolTimeout
    FAIL = "fail"  # raise PoolExhausted immediately


@dataclass(frozen=True)
class PoolConfig:
    min_size: int = 0
    max_size: int = 5
    checkout_timeout: float = 30.0
    max_lifetime: float | None = 1800.0
    idle_timeout: float | None = 600.0
    health_check_interval: float | None = 30.0  # validate on checkout if unchecked this long; 0 = always
    on_exhausted: ExhaustionPolicy = ExhaustionPolicy.WAIT
    shutdown_timeout: float = 10.0

    def __post_init__(self) -> None:
        def is_int(v: object) -> bool:
            return isinstance(v, int) and not isinstance(v, bool)

        if not is_int(self.max_size) or self.max_size < 1:
            raise ConfigurationError("pool: 'max_size' must be an integer >= 1")
        if not is_int(self.min_size) or not 0 <= self.min_size <= self.max_size:
            raise ConfigurationError("pool: 'min_size' must be an integer between 0 and max_size")
        if self.checkout_timeout < 0 or self.shutdown_timeout < 0:
            raise ConfigurationError("pool: timeouts must be >= 0")
        for name in ("max_lifetime", "idle_timeout"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ConfigurationError(f"pool: '{name}' must be > 0 or None")
        if self.health_check_interval is not None and self.health_check_interval < 0:
            raise ConfigurationError("pool: 'health_check_interval' must be >= 0 or None")
        object.__setattr__(self, "on_exhausted", ExhaustionPolicy(self.on_exhausted))
