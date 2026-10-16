from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.integrations.base import OutboundEmail, SentEmail
from app.integrations.http import HttpClient, IntegrationError
from app.integrations.microsoft.calendar import OutlookCalendarAdapter
from app.integrations.microsoft.graph import BASE_URL
from app.integrations.microsoft.mail import OutlookMailAdapter
from tests.integration.adapters.conftest import fixture

START = datetime(2026, 10, 16, tzinfo=UTC)
END = datetime(2026, 10, 18, tzinfo=UTC)
DELTA = f"{BASE_URL}/me/calendarView/delta"


@pytest.fixture
def cal(make_http: Callable[..., HttpClient]) -> OutlookCalendarAdapter:
    return OutlookCalendarAdapter(make_http("microsoft", BASE_URL))


@pytest.fixture
def mail(make_http: Callable[..., HttpClient]) -> OutlookMailAdapter:
    return OutlookMailAdapter(make_http("microsoft", BASE_URL), ceo_address="CEO@example.com")


def test_delta_sync_follows_next_link(
    mock_http: respx.MockRouter, cal: OutlookCalendarAdapter
) -> None:
    route = mock_http.get(url__startswith=DELTA).mock(
        side_effect=[
            httpx.Response(200, json=fixture("microsoft/calendar_delta_page1.json")),
            httpx.Response(200, json=fixture("microsoft/calendar_delta_page2.json")),
        ]
    )
    result = cal.sync_events(START, END)
    assert result.full is True
    assert result.cursor and result.cursor.endswith("deltatoken=d1")
    event, removed = result.records
    assert event.title == "4 pm vendor call"
    assert event.start == datetime(2026, 10, 16, 10, 30, tzinfo=UTC)
    assert event.attendees[0].response == "needsAction"
    assert event.organizer == "sales@vendor.com"
    assert removed.cancelled and removed.id == "AAMk-gone"
    assert route.calls[0].request.url.params["startDateTime"].startswith("2026-10-16T00:00:00")
    assert 'outlook.timezone="UTC"' in route.calls[0].request.headers["Prefer"]
    assert "skiptoken=page2" in str(route.calls[1].request.url)


def test_delta_link_reused_and_expired(
    mock_http: respx.MockRouter, cal: OutlookCalendarAdapter
) -> None:
    link = f"{DELTA}?$deltatoken=d1"
    mock_http.get(url__startswith=DELTA).mock(
        side_effect=[
            httpx.Response(410, json={"error": {"code": "syncStateNotFound"}}),
            httpx.Response(200, json=fixture("microsoft/calendar_delta_page2.json")),
        ]
    )
    result = cal.sync_events(START, END, cursor=link)
    assert result.full is True

    mock_http.get(url__startswith=DELTA).mock(return_value=httpx.Response(500))
    with pytest.raises(IntegrationError):
        OutlookCalendarAdapter(HttpClient("microsoft", BASE_URL, max_retries=0)).sync_events(
            START, END, cursor=link
        )


def test_delta_incremental(mock_http: respx.MockRouter, cal: OutlookCalendarAdapter) -> None:
    mock_http.get(url__startswith=DELTA).mock(
        return_value=httpx.Response(200, json=fixture("microsoft/calendar_delta_page2.json"))
    )
    assert cal.sync_events(START, END, cursor=f"{DELTA}?$deltatoken=d0").full is False


def test_decline_and_add_attendee(mock_http: respx.MockRouter, cal: OutlookCalendarAdapter) -> None:
    decline = mock_http.post(f"{BASE_URL}/me/events/AAMk-1/decline").mock(
        return_value=httpx.Response(202)
    )
    cal.decline("AAMk-1", "Rahul will cover this.")
    assert json.loads(decline.calls[0].request.content) == {
        "comment": "Rahul will cover this.",
        "sendResponse": True,
    }
    attendees = {"attendees": [{"emailAddress": {"address": "ceo@example.com"}}]}
    mock_http.get(f"{BASE_URL}/me/events/AAMk-1").mock(
        return_value=httpx.Response(200, json=attendees)
    )
    patch = mock_http.patch(f"{BASE_URL}/me/events/AAMk-1").mock(
        return_value=httpx.Response(200, json={})
    )
    cal.add_attendee("AAMk-1", "rahul@example.com")
    cal.add_attendee("AAMk-1", "CEO@example.com")
    assert patch.call_count == 1
    added = json.loads(patch.calls[0].request.content)["attendees"][-1]
    assert added == {"emailAddress": {"address": "rahul@example.com"}, "type": "required"}


def test_mail_actionable_and_replies(mock_http: respx.MockRouter, mail: OutlookMailAdapter) -> None:
    inbox = mock_http.get(f"{BASE_URL}/me/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(200, json=fixture("microsoft/messages.json"))
    )
    [msg] = mail.list_actionable(START)
    assert msg.sender == "hr@example.com" and msg.flagged
    assert msg.recipients == ("ceo@example.com",)
    assert (
        "receivedDateTime ge 2026-10-16T00:00:00Z" in inbox.calls[0].request.url.params["$filter"]
    )

    own = {
        "value": [
            {
                **fixture("microsoft/messages.json")["value"][0],
                "id": "mine",
                "from": {"emailAddress": {"address": "ceo@example.com"}},
            }
        ]
    }
    replies = mock_http.get(f"{BASE_URL}/me/messages").mock(
        side_effect=[
            httpx.Response(200, json=fixture("microsoft/messages.json")),
            httpx.Response(200, json=own),
        ]
    )
    assert [m.id for m in mail.list_thread_replies("conv-1", START)] == ["msg-1"]
    assert mail.list_thread_replies("o'brien", START) == []
    assert "conversationId eq 'o''brien'" in replies.calls[1].request.url.params["$filter"]


def test_mail_send_and_reply(mock_http: respx.MockRouter, mail: OutlookMailAdapter) -> None:
    send = mock_http.post(f"{BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
    reply = mock_http.post(f"{BASE_URL}/me/messages/msg-1/reply").mock(
        return_value=httpx.Response(202)
    )
    sent_items = mock_http.get(f"{BASE_URL}/me/mailFolders/SentItems/messages").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "other", "conversationId": "c0", "subject": "Older"},
                        {"id": "s1", "conversationId": "conv-9", "subject": "S"},
                    ]
                },
            ),
            httpx.Response(200, json={"value": []}),
            httpx.Response(404, json={}),
        ]
    )
    out = OutboundEmail(to=("rahul@example.com",), subject="S", text="plain", html="<b>x</b>")
    assert mail.send(out) == SentEmail("s1", "conv-9")
    assert sent_items.calls[0].request.url.params["$orderby"] == "sentDateTime desc"
    body = json.loads(send.calls[0].request.content)
    assert body["message"]["body"] == {"contentType": "HTML", "content": "<b>x</b>"}
    assert body["saveToSentItems"] is True
    assert mail.send(
        OutboundEmail(to=("r@example.com",), subject="S", text="plain", reply_to_message_id="msg-1")
    ) == SentEmail(None, None)
    assert mail.send(OutboundEmail(to=("r@example.com",), subject="S", text="x")) == SentEmail(
        None, None
    )
    assert json.loads(reply.calls[0].request.content)["message"]["body"]["contentType"] == "Text"
