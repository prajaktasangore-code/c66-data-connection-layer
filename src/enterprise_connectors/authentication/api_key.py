"""API key, bearer token and basic authentication for HTTP clients.

Each auth object is callable as a ``requests`` auth hook (``session.auth = ApiKeyAuth(...)``)
and exposes ``headers()`` for any other client. Reprs never show secrets.
"""

from __future__ import annotations

import base64
from typing import Any

from ..core.credentials import ApiKeyCredential, TokenCredential, UsernamePassword
from ..core.exceptions import ConfigurationError


class _HeaderAuth:
    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def __call__(self, request: Any) -> Any:
        request.headers.update(self.headers())
        return request

    def __repr__(self) -> str:
        return f"{type(self).__name__}(***)"


class ApiKeyAuth(_HeaderAuth):
    """Sends the key in a configurable header (default ``X-API-Key``), optionally with a prefix."""

    def __init__(self, credential: ApiKeyCredential, *, header_name: str = "X-API-Key", prefix: str | None = None):
        if not isinstance(credential, ApiKeyCredential):
            raise ConfigurationError("ApiKeyAuth requires an ApiKeyCredential")
        if not header_name or any(c in header_name for c in ":\r\n "):
            raise ConfigurationError("ApiKeyAuth: invalid header_name")
        self._credential = credential
        self.header_name = header_name
        self._prefix = prefix

    def headers(self) -> dict[str, str]:
        key = self._credential.api_key.get_secret_value()
        return {self.header_name: f"{self._prefix} {key}" if self._prefix else key}


class BearerTokenAuth(_HeaderAuth):
    """``Authorization: Bearer <token>``. Pass a TokenManager to get automatic refresh."""

    def __init__(self, token: TokenCredential | Any) -> None:
        self._token = token

    def headers(self) -> dict[str, str]:
        token = self._token.get_token() if hasattr(self._token, "get_token") else self._token
        return {"Authorization": f"Bearer {token.token.get_secret_value()}"}


class BasicAuth(_HeaderAuth):
    def __init__(self, credential: UsernamePassword) -> None:
        if not isinstance(credential, UsernamePassword):
            raise ConfigurationError("BasicAuth requires UsernamePassword credentials")
        self._credential = credential

    def headers(self) -> dict[str, str]:
        raw = f"{self._credential.username}:{self._credential.password.get_secret_value()}"
        return {"Authorization": f"Basic {base64.b64encode(raw.encode()).decode()}"}
