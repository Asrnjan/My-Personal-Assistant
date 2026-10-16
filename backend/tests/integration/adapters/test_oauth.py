from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import OAuthTokenRow
from app.integrations.http import AuthExpiredError, IntegrationError
from app.integrations.oauth import (
    OAuthManager,
    TokenSet,
    TokenStore,
    google_provider,
    microsoft_provider,
)
from app.security import TokenCipher

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(session_factory: sessionmaker[Session], clock: Clock) -> TokenStore:
    return TokenStore(session_factory, TokenCipher(Fernet.generate_key().decode()), clock)


@pytest.fixture
def make_manager(
    session_factory: sessionmaker[Session], store: TokenStore, clock: Clock
) -> Callable[..., OAuthManager]:
    def make(**overrides: str) -> OAuthManager:
        settings = Settings(
            google_client_id=overrides.get("google_client_id", "gid"),
            google_client_secret=SecretStr("gsecret"),
            ms_client_id="mid",
            ms_client_secret=SecretStr("msecret"),
            ms_tenant_id="tenant-1",
        )
        return OAuthManager(
            {"google": google_provider(settings), "microsoft": microsoft_provider(settings)},
            store,
            session_factory,
            "https://assistant.example.com/",
            clock=clock,
        )

    return make


def test_cipher_round_trip_and_wrong_key() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode())
    secret = cipher.encrypt("ya29.token")
    assert secret != "ya29.token" and cipher.decrypt(secret) == "ya29.token"
    with pytest.raises(ValueError, match="FERNET_KEY"):
        TokenCipher(Fernet.generate_key().decode()).decrypt(secret)
    with pytest.raises(ValueError, match="not set"):
        TokenCipher("")


def test_tokens_encrypted_at_rest(
    store: TokenStore, session_factory: sessionmaker[Session]
) -> None:
    store.save("google", TokenSet("access-1", "refresh-1", None, "s"))
    with session_factory() as s:
        row = s.get(OAuthTokenRow, "google")
        assert row is not None
        assert "access-1" not in row.access_token_enc
        assert "refresh-1" not in (row.refresh_token_enc or "")
    loaded = store.load("google")
    assert loaded == TokenSet("access-1", "refresh-1", None, "s")
    store.save("google", TokenSet("access-2", None, None))
    assert store.load("google") == TokenSet("access-2", None, None, "")
    store.delete("google")
    assert store.load("google") is None


def test_authorization_url_and_callback(
    mock_http: respx.MockRouter, make_manager: Callable[..., OAuthManager], store: TokenStore
) -> None:
    manager = make_manager()
    url = urlparse(manager.authorization_url("google"))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com"
    assert q["redirect_uri"] == "https://assistant.example.com/oauth/google/callback"
    assert q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
    assert "gmail.send" in q["scope"]

    token = mock_http.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )
    )
    manager.handle_callback("google", "auth-code", q["state"])
    sent = parse_qs(token.calls[0].request.content.decode())
    assert sent["grant_type"] == ["authorization_code"] and sent["code_verifier"][0]
    saved = store.load("google")
    assert saved is not None and saved.refresh_token == "rt"
    assert manager.is_connected("google")

    with pytest.raises(IntegrationError, match="invalid or expired state"):
        manager.handle_callback("google", "auth-code", q["state"])  # state is one-time


def test_state_expiry_and_provider_mismatch(
    make_manager: Callable[..., OAuthManager], clock: Clock
) -> None:
    manager = make_manager()
    state = parse_qs(urlparse(manager.authorization_url("microsoft")).query)["state"][0]
    with pytest.raises(IntegrationError):
        manager.handle_callback("google", "c", state)
    state = parse_qs(urlparse(manager.authorization_url("google")).query)["state"][0]
    clock.now += timedelta(minutes=11)
    with pytest.raises(IntegrationError):
        manager.handle_callback("google", "c", state)


def test_unknown_or_unconfigured_provider(make_manager: Callable[..., OAuthManager]) -> None:
    with pytest.raises(IntegrationError, match="unknown provider"):
        make_manager().authorization_url("yahoo")
    with pytest.raises(IntegrationError, match="not configured"):
        make_manager(google_client_id="").authorization_url("google")
    assert "tenant-1" in make_manager().provider("microsoft").token_url


def test_access_token_refreshes_near_expiry(
    mock_http: respx.MockRouter,
    make_manager: Callable[..., OAuthManager],
    store: TokenStore,
    clock: Clock,
) -> None:
    manager = make_manager()
    store.save("google", TokenSet("old", "rt", clock.now + timedelta(minutes=30)))
    assert manager.access_token("google") == "old"

    mock_http.post(GOOGLE_TOKEN).mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    )
    clock.now += timedelta(minutes=29, seconds=30)
    assert manager.access_token("google") == "new"
    refreshed = store.load("google")
    assert refreshed is not None and refreshed.refresh_token == "rt"  # kept


def test_refresh_failures(
    mock_http: respx.MockRouter,
    make_manager: Callable[..., OAuthManager],
    store: TokenStore,
    clock: Clock,
) -> None:
    manager = make_manager()
    with pytest.raises(AuthExpiredError, match="not connected"):
        manager.access_token("google")
    assert manager.refresh("google") is False

    store.save("google", TokenSet("old", None, clock.now - timedelta(seconds=1)))
    with pytest.raises(AuthExpiredError, match="reconnect"):
        manager.access_token("google")

    store.save("google", TokenSet("old", "rt", None))
    mock_http.post(GOOGLE_TOKEN).mock(return_value=httpx.Response(400, text="invalid_grant"))
    assert manager.refresh("google") is False
    mock_http.post(GOOGLE_TOKEN).mock(return_value=httpx.Response(500))
    with pytest.raises(IntegrationError, match="HTTP 500"):
        manager.refresh("google")
    mock_http.post(GOOGLE_TOKEN).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(IntegrationError, match="unreachable"):
        manager.refresh("google")
    mock_http.post(GOOGLE_TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "n"}))
    assert manager.refresh("google") is True
    latest = store.load("google")
    assert latest is not None and latest.expires_at is None
