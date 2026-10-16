from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.integrations.base import OutboundEmail
from app.integrations.google.calendar import BASE_URL as CAL
from app.integrations.google.calendar import GoogleCalendarAdapter
from app.integrations.google.gmail import BASE_URL as GMAIL
from app.integrations.google.gmail import GmailAdapter
from app.integrations.http import HttpClient, IntegrationError
from app.integrations.mime import b64url
from tests.integration.adapters.conftest import fixture

START = datetime(2026, 10, 16, tzinfo=UTC)
END = datetime(2026, 10, 18, tzinfo=UTC)
EVENTS = f"{CAL}/calendars/primary/events"


@pytest.fixture
def gcal(make_http: Callable[..., HttpClient]) -> GoogleCalendarAdapter:
    return GoogleCalendarAdapter(make_http("google", CAL))


@pytest.fixture
def gmail(make_http: Callable[..., HttpClient]) -> GmailAdapter:
    return GmailAdapter(make_http("google", GMAIL))


def test_full_sync_paginates_and_normalises(
    mock_http: respx.MockRouter, gcal: GoogleCalendarAdapter
) -> None:
    route = mock_http.get(EVENTS).mock(
        side_effect=[
            httpx.Response(200, json=fixture("google/events_page1.json")),
            httpx.Response(200, json=fixture("google/events_page2.json")),
        ]
    )
    result = gcal.sync_events(START, END)
    assert result.full is True
    assert result.cursor == "sync-1"
    assert [e.id for e in result.records] == ["evt-budget", "evt-offsite"]
    budget, offsite = result.records
    assert budget.start == datetime(2026, 10, 16, 10, 30, tzinfo=UTC)
    assert budget.organizer == "cfo@example.com"
    assert budget.attendees[0].is_self and budget.attendees[0].response == "needsAction"
    assert budget.ical_uid == "budget@example.com"
    assert offsite.all_day and offsite.start == datetime(2026, 10, 17, tzinfo=UTC)
    first = route.calls[0].request.url.params
    assert first["singleEvents"] == "true" and "timeMin" in first
    assert route.calls[1].request.url.params["pageToken"] == "page-2"
    assert route.calls[0].request.headers["Authorization"] == "Bearer test"


def test_incremental_sync_and_expired_token(
    mock_http: respx.MockRouter, gcal: GoogleCalendarAdapter
) -> None:
    mock_http.get(EVENTS, params={"syncToken": "sync-1"}).mock(
        return_value=httpx.Response(200, json=fixture("google/events_incremental.json"))
    )
    result = gcal.sync_events(START, END, cursor="sync-1")
    assert result.full is False
    assert result.cursor == "sync-2"
    assert result.records[0].cancelled

    mock_http.get(EVENTS, params={"syncToken": "stale"}).mock(return_value=httpx.Response(410))
    mock_http.get(EVENTS, params={"singleEvents": "true"}).mock(
        return_value=httpx.Response(200, json=fixture("google/events_page2.json"))
    )
    resynced = gcal.sync_events(START, END, cursor="stale")
    assert resynced.full is True and resynced.cursor == "sync-1"


def test_sync_other_errors_propagate(
    mock_http: respx.MockRouter, gcal: GoogleCalendarAdapter
) -> None:
    mock_http.get(EVENTS).mock(return_value=httpx.Response(400))
    with pytest.raises(IntegrationError):
        gcal.sync_events(START, END, cursor="x")


def test_decline_sets_self_response_with_note(
    mock_http: respx.MockRouter, gcal: GoogleCalendarAdapter
) -> None:
    event = fixture("google/events_page1.json")["items"][0]
    mock_http.get(f"{EVENTS}/evt-budget").mock(return_value=httpx.Response(200, json=event))
    patch = mock_http.patch(f"{EVENTS}/evt-budget").mock(return_value=httpx.Response(200, json={}))
    gcal.decline("evt-budget", "Rahul will attend for me.")
    sent = json.loads(patch.calls[0].request.content)
    me = next(a for a in sent["attendees"] if a.get("self"))
    assert me["responseStatus"] == "declined" and me["comment"] == "Rahul will attend for me."
    assert patch.calls[0].request.url.params["sendUpdates"] == "all"


def test_decline_requires_ceo_attendee(
    mock_http: respx.MockRouter, gcal: GoogleCalendarAdapter
) -> None:
    mock_http.get(f"{EVENTS}/e").mock(return_value=httpx.Response(200, json={"attendees": []}))
    with pytest.raises(IntegrationError, match="not an attendee"):
        gcal.decline("e", "x")


def test_add_attendee_is_idempotent(
    mock_http: respx.MockRouter, gcal: GoogleCalendarAdapter
) -> None:
    event = fixture("google/events_page1.json")["items"][0]
    mock_http.get(f"{EVENTS}/evt-budget").mock(return_value=httpx.Response(200, json=event))
    patch = mock_http.patch(f"{EVENTS}/evt-budget").mock(return_value=httpx.Response(200, json={}))
    gcal.add_attendee("evt-budget", "rahul@example.com")
    assert "rahul@example.com" in patch.calls[0].request.content.decode()
    gcal.add_attendee("evt-budget", "CFO@example.com")
    assert patch.call_count == 1


def test_gmail_actionable(mock_http: respx.MockRouter, gmail: GmailAdapter) -> None:
    listing = mock_http.get(f"{GMAIL}/messages").mock(
        side_effect=[
            httpx.Response(200, json={**fixture("google/gmail_list.json"), "nextPageToken": "p2"}),
            httpx.Response(200, json={"messages": []}),
        ]
    )
    mock_http.get(f"{GMAIL}/messages/m1").mock(
        return_value=httpx.Response(200, json=fixture("google/gmail_m1.json"))
    )
    mock_http.get(f"{GMAIL}/messages/m2").mock(
        return_value=httpx.Response(200, json=fixture("google/gmail_m2.json"))
    )
    msgs = gmail.list_actionable(START)
    assert listing.calls[0].request.url.params["q"].endswith(f"after:{int(START.timestamp())}")
    m1, m2 = msgs
    assert m1.subject == "Vendor contract review"
    assert m1.sender == "meera@example.com"
    assert m1.recipients == ("ceo@example.com", "ops@example.com")
    assert m1.flagged and not m2.flagged
    assert m1.received_at is not None and m1.received_at.tzinfo == UTC


def test_gmail_thread_replies_skip_sent_and_old(
    mock_http: respx.MockRouter, gmail: GmailAdapter
) -> None:
    mock_http.get(f"{GMAIL}/threads/t1").mock(
        return_value=httpx.Response(200, json=fixture("google/gmail_thread.json"))
    )
    replies = gmail.list_thread_replies("t1", datetime(2026, 10, 1, tzinfo=UTC))
    assert [r.id for r in replies] == ["r1"]
    assert replies[0].snippet == "Accepted, on it."


def test_gmail_send_reply_keeps_thread(mock_http: respx.MockRouter, gmail: GmailAdapter) -> None:
    mock_http.get(f"{GMAIL}/messages/m1").mock(
        return_value=httpx.Response(200, json=fixture("google/gmail_m1.json"))
    )
    send = mock_http.post(f"{GMAIL}/messages/send").mock(
        return_value=httpx.Response(200, json={"id": "sent-1", "threadId": "t1"})
    )
    sent = gmail.send(
        OutboundEmail(
            to=("rahul@example.com",),
            subject="Delegated: Vendor contract review",
            text="Please review.",
            html="<p>Please review.</p>",
            reply_to_message_id="m1",
        )
    )
    assert sent.message_id == "sent-1" and sent.thread_id == "t1"
    payload = json.loads(send.calls[0].request.content)
    assert payload["threadId"] == "t1"
    import base64

    raw = base64.urlsafe_b64decode(payload["raw"] + "==").decode()
    assert "In-Reply-To: <abc123@mail.example.com>" in raw
    assert "text/plain" in raw and "text/html" in raw
    assert b64url(b"\xff") == "_w"


def test_gmail_send_new_message(mock_http: respx.MockRouter, gmail: GmailAdapter) -> None:
    send = mock_http.post(f"{GMAIL}/messages/send").mock(
        return_value=httpx.Response(200, json={"id": "sent-2"})
    )
    sent = gmail.send(
        OutboundEmail(to=("a@example.com",), subject="Hi", text="t", cc=("b@example.com",))
    )
    assert sent.thread_id is None
    payload = json.loads(send.calls[0].request.content)
    assert "threadId" not in payload
