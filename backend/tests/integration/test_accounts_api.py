from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.db.models import ProviderAppRow
from app.integrations.microsoft.mail import OutlookMailAdapter
from app.integrations.oauth import TokenSet
from tests.conftest import API_SECRET

MS_TOKEN = "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    configured = settings.model_copy(
        update={"fernet_key": SecretStr(Fernet.generate_key().decode())}
    )
    app = create_app(configured)
    with TestClient(app) as c:
        token = c.post("/auth/token", json={"secret": API_SECRET, "client_name": "t"}).json()
        c.headers["Authorization"] = f"Bearer {token['access_token']}"
        yield c
    app.state.engine.dispose()


def _accounts(c: TestClient) -> dict[str, dict[str, object]]:
    body = c.get("/accounts").json()
    assert body["oauth_ready"] is True
    return {a["id"]: a for a in body["accounts"]}


def test_accounts_need_sign_in(client: TestClient) -> None:
    assert client.get("/accounts").status_code == 401


def test_without_fernet_key_nothing_can_connect(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = client.get("/accounts", headers=auth_headers).json()
    assert body["oauth_ready"] is False
    assert {a["id"] for a in body["accounts"]} == {"google", "outlook", "teams"}
    assert not any(a["ready"] for a in body["accounts"])
    put = client.put(
        "/accounts/apps/microsoft",
        json={"client_id": "x", "client_secret": "y"},
        headers=auth_headers,
    )
    assert put.status_code == 503 and "FERNET_KEY" in put.json()["detail"]


def test_connect_outlook_and_teams_from_the_app(app_client: TestClient) -> None:
    c = app_client
    accounts = _accounts(c)
    assert accounts["outlook"] == {
        "id": "outlook",
        "name": "Outlook",
        "description": "Outlook email and calendar",
        "provider": "microsoft",
        "connected": False,
        "ready": False,
    }
    blocked = c.post("/accounts/outlook/connect")
    assert blocked.status_code == 400 and "one-time setup" in blocked.json()["detail"]

    # One-time setup: the app keys are saved from the app, encrypted.
    saved = c.put(
        "/accounts/apps/microsoft",
        json={
            "client_id": " ms-id ",
            "client_secret": "ms-secret",
            "tenant_id": "contoso.onmicrosoft.com",
        },
    )
    assert saved.status_code == 204
    with c.app.state.session_factory() as s:  # type: ignore[attr-defined]
        row = s.get(ProviderAppRow, "microsoft")
        assert row is not None and row.client_id == "ms-id"
        assert "ms-secret" not in row.client_secret_enc
    body = c.get("/accounts").json()
    ms = next(p for p in body["providers"] if p["id"] == "microsoft")
    assert ms["configured"] and ms["keys_source"] == "app"
    assert ms["tenant_id"] == "contoso.onmicrosoft.com"
    assert ms["redirect_uri"].endswith("/oauth/microsoft/callback")
    assert _accounts(c)["teams"]["ready"] is True

    # Outlook: Microsoft's sign-in page on the saved tenant, without Teams scopes.
    url = c.post("/accounts/outlook/connect").json()["authorization_url"]
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert parsed.netloc == "login.microsoftonline.com"
    assert parsed.path.startswith("/contoso.onmicrosoft.com/")
    assert q["client_id"] == "ms-id" and "Mail.ReadWrite" in q["scope"]
    assert "Chat.ReadWrite" not in q["scope"]

    with respx.mock() as router:
        router.post(MS_TOKEN).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 3600,
                    "scope": "https://graph.microsoft.com/Mail.ReadWrite User.Read",
                },
            )
        )
        done = c.get(f"/oauth/microsoft/callback?code=c&state={q['state']}")
    assert done.status_code == 200
    assert "/?connected=microsoft" in done.text and "Back to your assistant" in done.text
    accounts = _accounts(c)
    assert accounts["outlook"]["connected"] is True
    assert accounts["teams"]["connected"] is False

    # The connected mailbox is used even though config.yaml only lists Google.
    built = c.app.state.adapters_factory()  # type: ignore[attr-defined]
    assert any(isinstance(m, OutlookMailAdapter) for m in built.mail)

    # Teams asks for its scopes on top of Outlook's.
    teams_url = c.post("/accounts/teams/connect").json()["authorization_url"]
    scope = parse_qs(urlparse(teams_url).query)["scope"][0]
    assert "Chat.ReadWrite" in scope and "Mail.ReadWrite" in scope
    oauth = c.app.state.oauth  # type: ignore[attr-defined]
    oauth._store.save(
        "microsoft", TokenSet("at", "rt", None, "Mail.ReadWrite Chat.ReadWrite User.Read")
    )
    assert _accounts(c)["teams"]["connected"] is True
    # Reconnecting Outlook keeps the Teams scopes it already has.
    again = c.post("/accounts/outlook/connect").json()["authorization_url"]
    assert "Chat.ReadWrite" in parse_qs(urlparse(again).query)["scope"][0]

    assert c.delete("/accounts/teams").status_code == 204
    accounts = _accounts(c)
    assert not accounts["teams"]["connected"] and not accounts["outlook"]["connected"]


def test_unknown_accounts_and_providers(app_client: TestClient) -> None:
    c = app_client
    assert c.post("/accounts/myspace/connect").status_code == 404
    assert c.delete("/accounts/myspace").status_code == 404
    assert (
        c.put("/accounts/apps/yahoo", json={"client_id": "a", "client_secret": "b"}).status_code
        == 404
    )
    assert (
        c.put("/accounts/apps/google", json={"client_id": "", "client_secret": "b"}).status_code
        == 422
    )


def test_env_keys_still_work(settings: Settings) -> None:
    from app.main import create_app

    configured = settings.model_copy(
        update={
            "fernet_key": SecretStr(Fernet.generate_key().decode()),
            "google_client_id": "gid",
            "google_client_secret": SecretStr("gs"),
        }
    )
    app = create_app(configured)
    with TestClient(app) as c:
        token = c.post("/auth/token", json={"secret": API_SECRET, "client_name": "t"}).json()
        c.headers["Authorization"] = f"Bearer {token['access_token']}"
        google = next(p for p in c.get("/accounts").json()["providers"] if p["id"] == "google")
        assert google["keys_source"] == "env" and google["configured"]
        url = c.post("/accounts/google/connect").json()["authorization_url"]
        assert url.startswith("https://accounts.google.com/")
    app.state.engine.dispose()
