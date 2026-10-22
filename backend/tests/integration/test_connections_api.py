from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.db.models import AppConnectionRow
from tests.conftest import API_SECRET


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    configured = settings.model_copy(
        update={"fernet_key": SecretStr(Fernet.generate_key().decode()), "telegram_polling": False}
    )
    app = create_app(configured)
    with TestClient(app) as c:
        token = c.post("/auth/token", json={"secret": API_SECRET, "client_name": "t"}).json()
        c.headers["Authorization"] = f"Bearer {token['access_token']}"
        c.app_state = app.state  # type: ignore[attr-defined]
        yield c
    app.state.engine.dispose()


def _apps(c: TestClient) -> dict[str, dict[str, object]]:
    body = c.get("/connections").json()
    assert body["ready"] is True
    return {a["id"]: a for a in body["apps"]}


def test_needs_sign_in(client: TestClient) -> None:
    assert client.get("/connections").status_code == 401


def test_without_fernet_key_nothing_can_connect(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/connections", headers=auth_headers).json() == {"ready": False, "apps": []}
    put = client.put("/connections/claude", json={"values": {}}, headers=auth_headers)
    assert put.status_code == 503


def test_lists_every_app_not_connected(app_client: TestClient) -> None:
    apps = _apps(app_client)
    assert set(apps) == {"claude", "jira", "asana", "clickup", "telegram", "slack", "teams-webhook"}
    assert not any(a["connected"] for a in apps.values())


def test_unknown_app(app_client: TestClient) -> None:
    assert app_client.put("/connections/nope", json={"values": {}}).status_code == 404
    assert app_client.delete("/connections/nope").status_code == 404


@respx.mock
def test_claude_key_is_checked_saved_encrypted_and_applied(app_client: TestClient) -> None:
    route = respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    r = app_client.put("/connections/claude", json={"values": {"api_key": "sk-ant-abc123"}})
    assert r.status_code == 200, r.text
    assert route.calls.last.request.headers["x-api-key"] == "sk-ant-abc123"
    body = r.json()
    assert body["app"]["connected"] is True and body["app"]["source"] == "app"
    # The secret never comes back.
    assert "sk-ant-abc123" not in r.text
    assert app_client.app_state.settings.anthropic_api_key.get_secret_value() == "sk-ant-abc123"
    with app_client.app_state.session_factory() as s:
        row = s.get(AppConnectionRow, "claude")
        assert row is not None and "sk-ant-abc123" not in row.data_enc


def test_claude_key_shape_is_checked_before_any_call(app_client: TestClient) -> None:
    r = app_client.put("/connections/claude", json={"values": {"api_key": "nope"}})
    assert r.status_code == 400 and "sk-ant-" in r.json()["detail"]
    assert not _apps(app_client)["claude"]["connected"]


def test_missing_fields_are_named(app_client: TestClient) -> None:
    r = app_client.put("/connections/jira", json={"values": {"email": "a@b.co"}})
    assert r.status_code == 400
    assert "Your Jira address" in r.json()["detail"] and "API token" in r.json()["detail"]


@respx.mock
def test_rejected_details_are_not_saved(app_client: TestClient) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/myself").mock(return_value=httpx.Response(401))
    r = app_client.put(
        "/connections/jira",
        json={
            "values": {
                "base_url": "https://acme.atlassian.net/",
                "email": "a@b.co",
                "api_token": "bad",
            }
        },
    )
    assert r.status_code == 400 and "didn't accept" in r.json()["detail"]
    assert not _apps(app_client)["jira"]["connected"]


@respx.mock
def test_jira_connects_and_switches_the_adapter_on(app_client: TestClient) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/myself").mock(
        return_value=httpx.Response(200, json={"displayName": "Dana"})
    )
    r = app_client.put(
        "/connections/jira",
        json={
            "values": {
                "base_url": "https://acme.atlassian.net/",
                "email": "a@b.co",
                "api_token": "tok",
            }
        },
    )
    assert r.status_code == 200 and "Dana" in r.json()["message"]
    state = app_client.app_state
    assert state.settings.jira_base_url == "https://acme.atlassian.net"
    built = state.adapters_factory()
    assert len(built.tasks) == 1  # enabled by the credentials, not config.yaml

    app_client.delete("/connections/jira")
    assert not _apps(app_client)["jira"]["connected"]
    assert state.adapters_factory().tasks == []


@respx.mock
def test_asana_and_clickup_find_their_own_ids(app_client: TestClient) -> None:
    respx.get("https://app.asana.com/api/1.0/users/me").mock(
        return_value=httpx.Response(
            200, json={"data": {"name": "Dana", "workspaces": [{"gid": "77", "name": "Acme"}]}}
        )
    )
    assert app_client.put("/connections/asana", json={"values": {"token": "t"}}).status_code == 200
    assert app_client.app_state.settings.asana_workspace_gid == "77"

    respx.get("https://api.clickup.com/api/v2/user").mock(
        return_value=httpx.Response(200, json={"user": {"id": 5, "username": "dana"}})
    )
    respx.get("https://api.clickup.com/api/v2/team").mock(
        return_value=httpx.Response(200, json={"teams": [{"id": "9", "name": "Acme"}]})
    )
    assert (
        app_client.put("/connections/clickup", json={"values": {"token": "pk_1"}}).status_code
        == 200
    )
    s = app_client.app_state.settings
    assert (s.clickup_team_id, s.clickup_user_id) == ("9", "5")


@respx.mock
def test_telegram_finds_the_chat_after_start(app_client: TestClient) -> None:
    respx.get("https://api.telegram.org/bot1:abc/getMe").mock(
        return_value=httpx.Response(200, json={"result": {"username": "my_bot"}})
    )
    updates = respx.get("https://api.telegram.org/bot1:abc/getUpdates")
    updates.mock(return_value=httpx.Response(200, json={"result": []}))
    first = app_client.put("/connections/telegram", json={"values": {"bot_token": "1:abc"}})
    assert first.status_code == 400 and "press Start" in first.json()["detail"]

    updates.mock(
        return_value=httpx.Response(
            200, json={"result": [{"message": {"chat": {"id": 4242, "type": "private"}}}]}
        )
    )
    second = app_client.put("/connections/telegram", json={"values": {"bot_token": "1:abc"}})
    assert second.status_code == 200, second.text
    assert app_client.app_state.settings.telegram_ceo_chat_id == "4242"
    assert app_client.app_state.telegram_bot is not None  # started without a restart
    app_client.delete("/connections/telegram")
    assert app_client.app_state.telegram_bot is None


@respx.mock
def test_slack_looks_up_the_member_id_from_the_ceo_email(app_client: TestClient) -> None:
    app_client.app_state.settings.ceo_email = "ceo@acme.com"
    respx.get("https://slack.com/api/auth.test").mock(
        return_value=httpx.Response(200, json={"ok": True, "team": "Acme"})
    )
    respx.get("https://slack.com/api/users.lookupByEmail").mock(
        return_value=httpx.Response(200, json={"ok": True, "user": {"id": "U42"}})
    )
    r = app_client.put("/connections/slack", json={"values": {"bot_token": "xoxb-1"}})
    assert r.status_code == 200
    assert app_client.app_state.settings.slack_ceo_user_id == "U42"


def test_teams_webhook_must_be_https(app_client: TestClient) -> None:
    bad = app_client.put("/connections/teams-webhook", json={"values": {"webhook_url": "x"}})
    assert bad.status_code == 400
    ok = app_client.put(
        "/connections/teams-webhook",
        json={"values": {"webhook_url": "https://x.webhook.office.com/a"}},
    )
    assert ok.status_code == 200


@respx.mock
def test_saved_values_survive_a_restart_and_beat_env(settings: Settings) -> None:
    from app.main import create_app

    key = SecretStr(Fernet.generate_key().decode())
    cfg = settings.model_copy(update={"fernet_key": key})
    first = create_app(cfg)
    respx.get("https://api.anthropic.com/v1/models").mock(return_value=httpx.Response(200, json={}))
    with TestClient(first) as c:
        token = c.post("/auth/token", json={"secret": API_SECRET, "client_name": "t"}).json()
        c.headers["Authorization"] = f"Bearer {token['access_token']}"
        assert (
            c.put("/connections/claude", json={"values": {"api_key": "sk-ant-kept"}}).status_code
            == 200
        )
    second_settings = settings.model_copy(
        update={"fernet_key": key, "anthropic_api_key": SecretStr("sk-ant-from-env")}
    )
    second = create_app(second_settings)
    assert second_settings.anthropic_api_key.get_secret_value() == "sk-ant-kept"
    first.state.engine.dispose()
    second.state.engine.dispose()
