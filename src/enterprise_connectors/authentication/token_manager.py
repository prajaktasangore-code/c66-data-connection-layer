"""Token caching, refresh and rotation without exposing secret material.

One TokenManager per credential identity. Never share one between tenants or users.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace

from ..core.credentials import TokenCredential
from ..core.exceptions import AuthenticationError, ConnectorError
from ..utils.logging import get_logger

_log = get_logger(__name__)

TokenFetcher = Callable[[TokenCredential | None], TokenCredential]


class TokenManager:
    """Returns a valid token, refreshing it ``refresh_skew`` seconds before expiry.

    ``fetcher(current)`` obtains a new token; ``current`` is the previous token (or None),
    so refresh-token grants can use it. Refresh is single-flight: concurrent callers wait
    for one fetch instead of stampeding the token endpoint.
    """

    def __init__(
        self,
        fetcher: TokenFetcher,
        *,
        refresh_skew: float = 60.0,
        initial: TokenCredential | None = None,
        clock: Callable[[], float] = time.time,
        name: str = "token",
    ) -> None:
        self._fetcher = fetcher
        self._skew = refresh_skew
        self._token = initial
        self._clock = clock
        self._name = name
        self._lock = threading.Lock()

    def _is_fresh(self, token: TokenCredential | None) -> bool:
        if token is None:
            return False
        return token.expires_at is None or self._clock() < token.expires_at - self._skew

    def get_token(self) -> TokenCredential:
        token = self._token
        if token is not None and self._is_fresh(token):
            return token
        with self._lock:
            token = self._token
            if token is not None and self._is_fresh(token):  # another thread refreshed while we waited
                return token
            try:
                new = self._fetcher(self._token)
            except ConnectorError:
                raise
            except Exception as exc:
                raise AuthenticationError(f"Could not refresh {self._name}", driver_error=type(exc).__name__) from None
            if not isinstance(new, TokenCredential):
                raise AuthenticationError(f"{self._name} fetcher did not return a TokenCredential")
            self._token = new
            _log.debug("%s refreshed (expires_at=%s)", self._name, new.expires_at)
            return new

    def invalidate(self) -> None:
        """Force a refresh on next use (e.g. after the source rejected the token with 401)."""
        with self._lock:
            if self._token is not None:
                self._token = replace(self._token, expires_at=0.0)  # keeps refresh_token on OAuth tokens

    def __repr__(self) -> str:
        return f"TokenManager(name={self._name!r})"
