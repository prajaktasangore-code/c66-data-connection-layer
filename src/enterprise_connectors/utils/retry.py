"""Retry with backoff for connection establishment only.

Only transient failures (unreachable, timeout) are retried by default; authentication,
authorization and configuration errors fail immediately.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ..core import exceptions as errors

T = TypeVar("T")

RETRYABLE: tuple[type[BaseException], ...] = (errors.ConnectionError, errors.ConnectionTimeout)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff: float = 0.5
    max_backoff: float = 8.0
    multiplier: float = 2.0
    jitter: float = 0.1

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise errors.ConfigurationError("retry: 'max_attempts' must be an integer >= 1")
        if self.initial_backoff < 0 or self.max_backoff < 0:
            raise errors.ConfigurationError("retry: backoff values must be >= 0")
        if self.multiplier < 1:
            raise errors.ConfigurationError("retry: 'multiplier' must be >= 1")
        if not 0 <= self.jitter <= 1:
            raise errors.ConfigurationError("retry: 'jitter' must be between 0 and 1")

    def backoff(self, attempt: int) -> float:
        """Delay after the given (1-based) failed attempt."""
        base = min(self.max_backoff, self.initial_backoff * self.multiplier ** (attempt - 1))
        return max(0.0, base * (1 + random.uniform(-self.jitter, self.jitter)))  # noqa: S311 - not crypto


NO_RETRY = RetryPolicy(max_attempts=1)


def retry_call(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    retry_on: tuple[type[BaseException], ...] = RETRYABLE,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call ``fn`` until it succeeds, a non-retryable error is raised, or attempts run out."""
    attempt = 1
    while True:
        try:
            return fn()
        except retry_on as exc:
            if attempt >= policy.max_attempts:
                raise
            delay = policy.backoff(attempt)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
            attempt += 1
