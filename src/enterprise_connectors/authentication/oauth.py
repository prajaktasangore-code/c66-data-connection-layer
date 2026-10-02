"""OAuth 2.0 token acquisition: client credentials, refresh token, and authorization-code (PKCE) exchange.

The interactive browser redirect/callback belongs to the calling application. This module
only generates PKCE values and performs token-endpoint requests. Error messages include
the OAuth ``error`` code only, never the response body or any token.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from ..core.credentials import OAuthClientCredentials, SecretStr, TokenCredential
from ..core.exceptions import AuthenticationError, AuthorizationError, ConfigurationError, map_driver_error
from .token_manager import TokenManager

_CLIENT_AUTH_METHODS = ("client_secret_post", "client_secret_basic")


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the S256 PKCE method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@dataclass(frozen=True, repr=False)
class OAuthToken(TokenCredential):
    """A TokenCredential that also carries the refresh token and granted scope."""

    refresh_token: SecretStr | None = None
    scope: str | None = None

    _secret_fields: ClassVar[tuple[str, ...]] = ("token", "refresh_token")


class OAuthClient:
    def __init__(
        self,
        token_url: str,
        client: OAuthClientCredentials,
        *,
        auth_method: str = "client_secret_post",
        timeout: float = 10.0,
        session: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not token_url.startswith("https://") and not token_url.startswith("http://localhost"):
            raise ConfigurationError("OAuth token_url must use https")
        if auth_method not in _CLIENT_AUTH_METHODS:
            raise ConfigurationError(f"auth_method must be one of: {', '.join(_CLIENT_AUTH_METHODS)}")
        self.token_url = token_url
        self._client = client
        self._auth_method = auth_method
        self._timeout = timeout
        self._session = session
        self._clock = clock

    # -- grants ------------------------------------------------------------------------------

    def client_credentials(self, scope: str | None = None) -> OAuthToken:
        data = {"grant_type": "client_credentials"}
        if scope:
            data["scope"] = scope
        return self._request(data)

    def refresh(self, refresh_token: SecretStr | str | None = None) -> OAuthToken:
        token = refresh_token or self._client.refresh_token
        if token is None:
            raise AuthenticationError("No refresh token available")
        raw = token.get_secret_value() if isinstance(token, SecretStr) else token
        result = self._request({"grant_type": "refresh_token", "refresh_token": raw})
        if result.refresh_token is None:  # providers may omit it when it is not rotated
            result = replace(result, refresh_token=SecretStr(raw))
        return result

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthToken:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if code_verifier:
            data["code_verifier"] = code_verifier
        return self._request(data)

    def token_manager(self, *, scope: str | None = None, refresh_skew: float = 60.0) -> TokenManager:
        """A TokenManager that refreshes with the refresh token when available, else client credentials."""

        def fetch(current: TokenCredential | None) -> TokenCredential:
            refresh_token = getattr(current, "refresh_token", None) or self._client.refresh_token
            return self.refresh(refresh_token) if refresh_token else self.client_credentials(scope)

        return TokenManager(fetch, refresh_skew=refresh_skew, clock=self._clock, name=f"oauth:{self._client.client_id}")

    # -- transport ---------------------------------------------------------------------------

    def _get_session(self) -> Any:
        if self._session is None:
            try:
                import requests
            except ImportError:
                raise ConfigurationError(
                    'OAuth requires requests: pip install "enterprise-connectors[oauth]"'
                ) from None
            self._session = requests.Session()
        return self._session

    def _request(self, data: dict[str, str]) -> OAuthToken:
        body = dict(data)
        auth = None
        secret = self._client.client_secret.get_secret_value() if self._client.client_secret else None
        if self._auth_method == "client_secret_basic" and secret is not None:
            auth = (self._client.client_id, secret)
        else:
            body["client_id"] = self._client.client_id
            if secret is not None:
                body["client_secret"] = secret
        try:
            response = self._get_session().post(
                self.token_url, data=body, auth=auth, timeout=self._timeout, headers={"Accept": "application/json"}
            )
        except Exception as exc:
            raise map_driver_error(exc, "oauth") from None
        payload = self._json(response)
        if response.status_code != 200 or "access_token" not in payload:
            code = str(payload.get("error") or f"http_{response.status_code}")[:64]
            error_cls = AuthorizationError if response.status_code == 403 else AuthenticationError
            raise error_cls(f"OAuth token request failed: {code}", connector_type="oauth")
        expires_in = payload.get("expires_in")
        return OAuthToken(
            token=payload["access_token"],
            expires_at=self._clock() + float(expires_in) if expires_in is not None else None,
            refresh_token=payload.get("refresh_token"),
            scope=payload.get("scope"),
        )

    @staticmethod
    def _json(response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def __repr__(self) -> str:
        return f"OAuthClient(token_url={self.token_url!r}, client_id={self._client.client_id!r})"
