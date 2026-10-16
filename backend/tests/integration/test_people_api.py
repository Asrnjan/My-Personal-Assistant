from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.domain.enums import ActionKind, Source
from app.events import Event
from app.integrations.base import ExternalTask
from app.main import create_app
from tests.harness import Harness, at_ist, harness  # noqa: F401

NEW = {
    "id": "priya",
    "full_name": "Priya Iyer",
    "aliases": ["PI"],
    "email": "priya@example.com",
    "preferred_channels": ["email"],
    "ownership_areas": ["marketing"],
}


def seed_task(h: Harness) -> str:
    h.tasks.tasks = {"OPS-1": ExternalTask(Source.JIRA, "OPS-1", "Vendor review", due=at_ist(18))}
    h.sync()
    return h.item("Vendor review").id


def test_delegates_crud(harness: Harness) -> None:
    c = harness.client
    assert {d["id"] for d in c.get("/delegates").json()} == {"rahul", "rahul-m", "meera", "arjun"}
    assert c.post("/delegates", json=NEW).status_code == 201
    assert c.post("/delegates", json=NEW).status_code == 409
    assert c.get("/delegates/priya").json()["full_name"] == "Priya Iyer"
    update = {k: v for k, v in NEW.items() if k != "id"} | {"full_name": "Priya R. Iyer"}
    assert c.put("/delegates/priya", json=update).json()["full_name"] == "Priya R. Iyer"
    assert c.put("/delegates/nobody", json=update).status_code == 404
    assert c.get("/delegates/nobody").status_code == 404
    assert (
        c.post("/delegates", json={**NEW, "id": "bad", "email": "not-an-email"}).status_code == 422
    )

    item_id = seed_task(harness)
    harness.update_item("Vendor review", suggested_delegate_id="priya")
    action = harness.app.state.actions.propose(
        ActionKind.DELEGATE, item_id, "x", {"delegate_id": "priya"}
    )
    assert c.post(f"/actions/{action.id}/confirm").json()["ok"] is True
    assert c.delete("/delegates/priya").status_code == 204
    assert c.delete("/delegates/priya").status_code == 404
    assert harness.item("Vendor review").suggested_delegate_id is None
    followups = c.get("/followups").json()
    assert followups[0]["delegate_id"] is None and followups[0]["title"] == "Vendor review"
    assert c.get("/followups", params={"open": "true"}).json()[0]["state"] == "pending"


def test_preferences_api(harness: Harness) -> None:
    c = harness.client
    assert c.get("/preferences").json() == []
    created = c.post("/preferences", json={"rule": "Anything from the board is P1."})
    assert created.status_code == 201
    pref_id = created.json()["id"]
    assert [p["rule_text"] for p in c.get("/preferences").json()] == [
        "Anything from the board is P1."
    ]
    assert c.post("/preferences", json={"rule": ""}).status_code == 422
    assert c.delete(f"/preferences/{pref_id}").status_code == 204
    assert c.delete(f"/preferences/{pref_id}").status_code == 404
    with pytest.raises(ValueError, match="needs some text"):
        harness.app.state.learning.add_rule("   ")
    with pytest.raises(ValueError, match="not found"):
        harness.app.state.learning.set_band("missing", "P1_do_myself")


def test_actions_api(harness: Harness) -> None:
    c = harness.client
    item_id = seed_task(harness)
    action = harness.app.state.actions.propose(ActionKind.DROP, item_id, "Dropping. Confirm?")
    listed = c.get("/actions", params={"status": "pending"}).json()
    assert [a["id"] for a in listed] == [action.id]
    assert listed[0]["title"] == "Vendor review" and listed[0]["readback"] == "Dropping. Confirm?"
    cancelled = c.post(f"/actions/{action.id}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert c.post(f"/actions/{action.id}/confirm").json()["ok"] is False
    assert c.post("/actions/nope/confirm").status_code == 404
    assert c.post("/actions/nope/cancel").status_code == 404
    assert len(c.get("/actions").json()) == 1


def test_routes_require_auth(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as c:
        for method, path in [
            ("post", "/converse"),
            ("get", "/actions"),
            ("get", "/delegates"),
            ("get", "/followups"),
            ("get", "/preferences"),
        ]:
            assert getattr(c, method)(path).status_code == 401, path
    app.state.engine.dispose()


def test_websocket_push_and_converse(harness: Harness) -> None:
    token = harness.client.headers["Authorization"].split()[1]
    with harness.client.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        harness.app.state.events.publish(Event("notice", {"text": "Rahul accepted."}))
        assert ws.receive_json() == {
            "type": "event",
            "kind": "notice",
            "payload": {"text": "Rahul accepted."},
        }
        harness.app.state.jobs.morning_brief()
        pushed = ws.receive_json()
        assert pushed["kind"] == "morning" and pushed["payload"]["text"].startswith("Good morning")
        ws.send_json({"type": "converse", "text": "brief me", "conversation_id": "ws"})
        reply = ws.receive_json()
        assert reply["type"] == "reply" and reply["conversation_id"] == "ws"
        ws.send_json({"type": "nonsense"})
        assert ws.receive_json()["type"] == "error"
    # Header auth works too.
    with harness.client.websocket_connect(
        "/ws", headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_websocket_rejects_bad_tokens(harness: Harness) -> None:
    # The test client sends its own valid Authorization header unless overridden.
    for url, auth in [("/ws?token=wrong", "Bearer wrong"), ("/ws", ""), ("/ws", "Bearer nope")]:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            harness.client.websocket_connect(url, headers={"Authorization": auth}) as ws,
        ):
            ws.receive_json()
        assert exc.value.code == 1008


def test_websocket_rejects_non_json(harness: Harness) -> None:
    token = harness.client.headers["Authorization"].split()[1]
    with harness.client.websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
