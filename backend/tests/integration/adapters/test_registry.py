from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from app.config import AppConfig, Settings
from app.integrations.google.calendar import GoogleCalendarAdapter
from app.integrations.http import AuthExpiredError
from app.integrations.oauth import (
    OAuthManager,
    TokenSet,
    TokenStore,
    google_provider,
    microsoft_provider,
)
from app.integrations.registry import SyncStateStore, build_adapters
from app.main import create_app
from app.security import TokenCipher
from tests.conftest import API_SECRET

ALL = AppConfig.model_validate(
    {
        "adapters": {
            "calendar": ["google", "microsoft"],
            "email": ["google", "microsoft"],
            "tasks": ["jira", "asana", "clickup"],
        }
    }
)


def _oauth(session_factory: sessionmaker[Session]) -> tuple[OAuthManager, TokenStore]:
    settings = Settings(google_client_id="g", google_client_secret=SecretStr("s"))
    store = TokenStore(session_factory, TokenCipher(Fernet.generate_key().decode()))
    manager = OAuthManager(
        {"google": google_provider(settings), "microsoft": microsoft_provider(settings)},
        store,
        session_factory,
        "http://localhost",
    )
    return manager, store


def test_nothing_configured_lists_what_is_missing(
    session_factory: sessionmaker[Session],
) -> None:
    manager, _ = _oauth(session_factory)
    adapters = build_adapters(Settings(), ALL, manager)
    assert adapters.calendars == [] and adapters.mail == [] and adapters.tasks == []
    text = "\n".join(adapters.not_configured)
    assert "google: account not connected" in text
    assert "microsoft: account not connected" in text
    assert "JIRA_API_TOKEN" in text and "ASANA" in text and "CLICKUP" in text
    no_key = build_adapters(Settings(), ALL, None)
    assert "FERNET_KEY" in no_key.not_configured[0]


def test_everything_configured(session_factory: sessionmaker[Session]) -> None:
    manager, store = _oauth(session_factory)
    store.save("google", TokenSet("g-at", "g-rt", None))
    store.save("microsoft", TokenSet("m-at", "m-rt", None))
    settings = Settings(
        jira_base_url="https://acme.atlassian.net",
        jira_email="ceo@example.com",
        jira_api_token=SecretStr("j"),
        asana_access_token=SecretStr("a"),
        asana_workspace_gid="ws",
        clickup_api_token=SecretStr("c"),
        clickup_team_id="t",
        clickup_user_id="1",
    )
    adapters = build_adapters(settings, ALL, manager, sleep=lambda _s: None)
    assert adapters.not_configured == []
    assert [a.source.value for a in adapters.calendars] == ["gcal", "outlook"]
    assert [a.source.value for a in adapters.mail] == ["gmail", "outlook"]
    assert [a.source.value for a in adapters.tasks] == ["jira", "asana", "clickup"]

    gcal = adapters.calendars[0]
    assert isinstance(gcal, GoogleCalendarAdapter)
    with respx.mock() as router:
        route = router.get(url__startswith="https://www.googleapis.com/").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        router.get(url__startswith="https://app.asana.com/").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        from datetime import UTC, datetime

        gcal.sync_events(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
        adapters.tasks[1].list_open()
    assert route.calls[0].request.headers["Authorization"] == "Bearer g-at"


def test_oauth_adapter_401_without_refresh_token(session_factory: sessionmaker[Session]) -> None:
    manager, store = _oauth(session_factory)
    store.save("google", TokenSet("g-at", None, None))
    config = AppConfig.model_validate(
        {"adapters": {"calendar": ["google"], "email": [], "tasks": [], "notify": []}}
    )
    adapters = build_adapters(Settings(), config, manager, sleep=lambda _s: None)
    from datetime import UTC, datetime

    with respx.mock() as router:
        router.get(url__startswith="https://www.googleapis.com/").mock(
            return_value=httpx.Response(401)
        )
        with pytest.raises(AuthExpiredError):
            adapters.calendars[0].sync_events(
                datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
            )


def test_sync_state_store(session_factory: sessionmaker[Session]) -> None:
    store = SyncStateStore(session_factory)
    assert store.get("gcal", "events") is None
    store.set("gcal", "events", "t1")
    store.set("gcal", "events", "t2")
    assert store.get("gcal", "events") == "t2"
    store.set("gcal", "events", None)
    store.set("gcal", "events", None)
    assert store.get("gcal", "events") is None


@pytest.fixture
def oauth_client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(
        database_url=database_url,
        api_auth_token=SecretStr(API_SECRET),
        fernet_key=SecretStr(Fernet.generate_key().decode()),
        google_client_id="gid",
        google_client_secret=SecretStr("gs"),
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


def test_oauth_routes(oauth_client: TestClient) -> None:
    token = oauth_client.post(
        "/auth/token", json={"secret": API_SECRET, "client_name": "t"}
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert oauth_client.get("/oauth/google/start").status_code == 401
    start = oauth_client.get("/oauth/google/start", headers=auth)
    assert start.status_code == 200
    url = start.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/")
    assert oauth_client.get("/oauth/microsoft/start", headers=auth).status_code == 400

    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(url).query)["state"][0]
    with respx.mock() as router:
        router.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 60})
        )
        done = oauth_client.get(f"/oauth/google/callback?code=c&state={state}")
    assert done.status_code == 200 and "connected" in done.text
    assert oauth_client.get("/oauth/google/callback?error=access_denied").status_code == 400
    assert oauth_client.get("/oauth/google/callback").status_code == 400
    bad = oauth_client.get("/oauth/google/callback?code=c&state=nope")
    assert bad.status_code == 400 and "expired state" in bad.text


def test_oauth_routes_without_fernet_key(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.get("/oauth/google/start", headers=auth_headers).status_code == 503
    assert client.get("/oauth/google/callback?code=c&state=s").status_code == 503
