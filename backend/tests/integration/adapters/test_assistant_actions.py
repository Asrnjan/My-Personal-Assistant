"""Adapter calls behind the assistant's email, Teams and meeting actions."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.integrations.base import NewEvent
from app.integrations.google.calendar import BASE_URL as CAL
from app.integrations.google.calendar import GoogleCalendarAdapter
from app.integrations.google.gmail import BASE_URL as GMAIL
from app.integrations.google.gmail import GmailAdapter
from app.integrations.http import HttpClient
from app.integrations.microsoft.calendar import OutlookCalendarAdapter
from app.integrations.microsoft.graph import BASE_URL
from app.integrations.microsoft.mail import OutlookMailAdapter
from app.integrations.microsoft.teams import TeamsChatAdapter, plain_text
from tests.integration.adapters.conftest import fixture

START = datetime(2026, 10, 19, 9, 30, tzinfo=UTC)
END = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
MEETING = NewEvent("Budget review", START, END, ("meera@example.com",), "Agenda")


@pytest.fixture
def graph(make_http: Callable[..., HttpClient]) -> HttpClient:
    return make_http("microsoft", BASE_URL)


def _body(route: respx.Route, n: int = 0) -> dict[str, object]:
    body: dict[str, object] = json.loads(route.calls[n].request.content)
    return body


def test_outlook_search_and_get(mock_http: respx.MockRouter, graph: HttpClient) -> None:
    mail = OutlookMailAdapter(graph)
    message = {
        "id": "o-1",
        "conversationId": "conv",
        "subject": "Q3",
        "from": {"emailAddress": {"address": "CFO@acme.com"}},
        "bodyPreview": "numbers",
    }
    search = mock_http.get(f"{BASE_URL}/me/messages").mock(
        return_value=httpx.Response(200, json={"value": [message]})
    )
    found = mail.search('q3 "numbers"', limit=5)
    assert [m.sender for m in found] == ["cfo@acme.com"]
    params = search.calls[0].request.url.params
    assert params["$search"] == '"q3  numbers"' and params["$top"] == "5"

    mock_http.get(f"{BASE_URL}/me/messages/o-1").mock(
        return_value=httpx.Response(200, json=message)
    )
    assert mail.get("o-1").subject == "Q3"


def test_gmail_search_and_get(
    mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]
) -> None:
    gmail = GmailAdapter(make_http("google", GMAIL))
    listing = mock_http.get(f"{GMAIL}/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "m1"}]})
    )
    mock_http.get(f"{GMAIL}/messages/m1").mock(
        return_value=httpx.Response(200, json=fixture("google/gmail_m1.json"))
    )
    found = gmail.search("from:meera", limit=3)
    assert found[0].subject == "Vendor contract review"
    assert listing.calls[0].request.url.params["q"] == "from:meera"
    assert gmail.get("m1").sender == "meera@example.com"


def test_outlook_books_a_teams_meeting(mock_http: respx.MockRouter, graph: HttpClient) -> None:
    route = mock_http.post(f"{BASE_URL}/me/events").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "ev-1",
                "subject": "Budget review",
                "start": {"dateTime": "2026-10-19T09:30:00.0000000"},
                "end": {"dateTime": "2026-10-19T10:00:00.0000000"},
            },
        )
    )
    event = OutlookCalendarAdapter(graph).create_event(MEETING)
    assert event.id == "ev-1" and event.start == START
    body = _body(route)
    assert body["start"] == {"dateTime": "2026-10-19T09:30:00", "timeZone": "UTC"}
    assert body["isOnlineMeeting"] is True
    assert body["attendees"] == [
        {"emailAddress": {"address": "meera@example.com"}, "type": "required"}
    ]


def test_google_books_a_meet_meeting(
    mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]
) -> None:
    route = mock_http.post(f"{CAL}/calendars/primary/events").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "g-ev",
                "summary": "Budget review",
                "start": {"dateTime": "2026-10-19T09:30:00Z"},
                "end": {"dateTime": "2026-10-19T10:00:00Z"},
            },
        )
    )
    gcal = GoogleCalendarAdapter(make_http("google", CAL))
    assert gcal.create_event(MEETING).id == "g-ev"
    params = route.calls[0].request.url.params
    assert params["sendUpdates"] == "all" and params["conferenceDataVersion"] == "1"
    assert "createRequest" in _body(route)["conferenceData"]  # type: ignore[operator]

    gcal.create_event(NewEvent("Offline", START, END, online=False))
    assert "conferenceDataVersion" not in route.calls[1].request.url.params
    assert "conferenceData" not in _body(route, 1)


def test_teams_chats_messages_and_sending(mock_http: respx.MockRouter, graph: HttpClient) -> None:
    teams = TeamsChatAdapter(graph)
    me = mock_http.get(f"{BASE_URL}/me").mock(
        return_value=httpx.Response(200, json={"id": "me-id", "mail": "CEO@contoso.com"})
    )
    chats = mock_http.get(f"{BASE_URL}/me/chats").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "c-1",
                        "topic": None,
                        "members": [
                            {"userId": "me-id", "displayName": "CEO", "email": "ceo@contoso.com"},
                            {
                                "userId": "p",
                                "displayName": "Priya Raman",
                                "email": "Priya@contoso.com",
                            },
                        ],
                        "lastMessagePreview": {
                            "id": "m-9",
                            "createdDateTime": "2026-10-19T09:00:00Z",
                            "from": {"user": {"displayName": "Priya Raman"}},
                            "body": {"content": "<p>Demo&nbsp;at <b>3</b>?</p>"},
                        },
                    },
                    {"id": "c-2", "topic": "Launch", "members": []},
                ]
            },
        )
    )
    first, second = teams.recent_chats(limit=5)
    assert first.members == ("Priya Raman",) and first.member_emails == ("priya@contoso.com",)
    assert first.last_message is not None and first.last_message.text == "Demo at 3?"
    assert second.topic == "Launch" and second.last_message is None
    assert "lastMessagePreview" in chats.calls[0].request.url.params["$expand"]

    mock_http.get(f"{BASE_URL}/chats/c-1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "1",
                        "messageType": "message",
                        "createdDateTime": "2026-10-19T08:00:00Z",
                        "from": {"user": {"displayName": "CEO"}},
                        "body": {"content": "Hi"},
                    },
                    {"id": "2", "messageType": "systemEventMessage", "body": {"content": ""}},
                    {
                        "id": "3",
                        "messageType": "message",
                        "createdDateTime": "2026-10-19T09:00:00Z",
                        "from": {"user": {"displayName": "Priya Raman"}},
                        "body": {"content": "Yo"},
                    },
                ]
            },
        )
    )
    assert [m.text for m in teams.messages("c-1")] == ["Yo", "Hi"]

    created = mock_http.post(f"{BASE_URL}/chats").mock(
        return_value=httpx.Response(201, json={"id": "c-new"})
    )
    posted = mock_http.post(f"{BASE_URL}/chats/c-new/messages").mock(
        return_value=httpx.Response(201, json={"id": "m-1", "body": {"content": "Call me"}})
    )
    sent = teams.send_to_person("arjun@contoso.com", "Call me")
    assert sent.id == "m-1" and sent.text == "Call me"
    members = _body(created)["members"]
    assert [m["user@odata.bind"] for m in members] == [  # type: ignore[attr-defined]
        f"{BASE_URL}/users('me-id')",
        f"{BASE_URL}/users('arjun@contoso.com')",
    ]
    assert _body(posted) == {"body": {"contentType": "text", "content": "Call me"}}
    assert me.call_count == 1  # looked up once


def test_plain_text() -> None:
    assert plain_text("<div>Line one<br/>line &amp; two</div>") == "Line one line & two"
