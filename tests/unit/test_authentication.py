"""API key / bearer / basic auth, TokenManager and OAuthClient."""

from __future__ import annotations

import base64
import hashlib
import threading
from typing import Any

import pytest

from enterprise_connectors import (
    ApiKeyCredential,
    AuthenticationError,
    ConfigurationError,
    OAuthClientCredentials,
    TokenCredential,
    UsernamePassword,
)
from enterprise_connectors.authentication.api_key import ApiKeyAuth, BasicAuth, BearerTokenAuth
from enterprise_connectors.authentication.oauth import OAuthClient, generate_pkce_pair
from enterprise_connectors.authentication.token_manager import TokenManager
from tests.fakes import FakeClock


class _Request:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


def test_api_key_header_is_configurable() -> None:
    auth = ApiKeyAuth(ApiKeyCredential(api_key="k1"), header_name="X-Custom", prefix="Key")
    assert auth.headers() == {"X-Custom": "Key k1"}
    assert ApiKeyAuth(ApiKeyCredential(api_key="k1"))(_Request()).headers == {"X-API-Key": "k1"}
    assert "k1" not in repr(auth)
    with pytest.raises(ConfigurationError):
        ApiKeyAuth(ApiKeyCredential(api_key="k"), header_name="Bad: Header")


def test_basic_and_bearer() -> None:
    header = BasicAuth(UsernamePassword("u", "p")).headers()["Authorization"]
    assert base64.b64decode(header.split()[1]) == b"u:p"
    assert BearerTokenAuth(TokenCredential(token="t")).headers() == {"Authorization": "Bearer t"}


def test_token_manager_refreshes_near_expiry() -> None:
    clock = FakeClock()
    issued = []

    def fetch(current: TokenCredential | None) -> TokenCredential:
        issued.append(current)
        return TokenCredential(token=f"t{len(issued)}", expires_at=clock() + 100)

    manager = TokenManager(fetch, refresh_skew=10, clock=clock)
    assert manager.get_token().token.get_secret_value() == "t1"
    clock.advance(80)
    assert manager.get_token().token.get_secret_value() == "t1"
    clock.advance(15)  # within skew
    assert manager.get_token().token.get_secret_value() == "t2"
    assert issued[1] is not None  # previous token handed to the fetcher
    manager.invalidate()
    assert manager.get_token().token.get_secret_value() == "t3"


def test_token_manager_single_flight() -> None:
    calls = []
    gate = threading.Event()

    def fetch(current: TokenCredential | None) -> TokenCredential:
        calls.append(1)
        gate.wait(1)
        return TokenCredential(token="t", expires_at=None)

    manager = TokenManager(fetch)
    threads = [threading.Thread(target=manager.get_token) for _ in range(10)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1


def test_token_manager_wraps_fetch_errors() -> None:
    def fetch(current: TokenCredential | None) -> TokenCredential:
        raise RuntimeError("body contained refresh_token=abc")

    with pytest.raises(AuthenticationError) as info:
        TokenManager(fetch).get_token()
    assert "abc" not in str(info.value)


def test_pkce_pair() -> None:
    verifier, challenge = generate_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected and 43 <= len(verifier) <= 128


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


CLIENT = OAuthClientCredentials(client_id="cid", client_secret="csecret", refresh_token="r0")


def test_oauth_refresh_keeps_refresh_token_when_not_rotated() -> None:
    session = _Session(_Response(200, {"access_token": "a1", "expires_in": 3600}))
    client = OAuthClient("https://login.example.com/token", CLIENT, session=session, clock=lambda: 0)
    token = client.refresh()
    assert token.token.get_secret_value() == "a1" and token.expires_at == 3600
    assert token.refresh_token is not None and token.refresh_token.get_secret_value() == "r0"
    body = session.calls[0]["data"]
    assert body["grant_type"] == "refresh_token" and body["client_secret"] == "csecret"


def test_oauth_client_secret_basic() -> None:
    session = _Session(_Response(200, {"access_token": "a"}))
    OAuthClient("https://x/token", CLIENT, auth_method="client_secret_basic", session=session).client_credentials("s")
    call = session.calls[0]
    assert call["auth"] == ("cid", "csecret") and "client_secret" not in call["data"]


def test_oauth_code_exchange_with_pkce() -> None:
    session = _Session(_Response(200, {"access_token": "a", "refresh_token": "r1"}))
    verifier, _ = generate_pkce_pair()
    token = OAuthClient("https://x/token", CLIENT, session=session).exchange_code("code", "https://app/cb", verifier)
    assert session.calls[0]["data"]["code_verifier"] == verifier
    assert token.refresh_token is not None and token.refresh_token.get_secret_value() == "r1"


def test_oauth_error_reports_code_only() -> None:
    session = _Session(_Response(400, {"error": "invalid_grant", "error_description": "token r0 revoked"}))
    with pytest.raises(AuthenticationError) as info:
        OAuthClient("https://x/token", CLIENT, session=session).refresh()
    assert "invalid_grant" in str(info.value) and "r0" not in str(info.value)


def test_oauth_requires_https() -> None:
    with pytest.raises(ConfigurationError):
        OAuthClient("http://login.example.com/token", CLIENT)


def test_oauth_token_manager_uses_refresh_grant() -> None:
    session = _Session(_Response(200, {"access_token": "a1", "expires_in": 10}))
    manager = OAuthClient("https://x/token", CLIENT, session=session).token_manager()
    assert manager.get_token().token.get_secret_value() == "a1"
    assert session.calls[0]["data"]["grant_type"] == "refresh_token"
