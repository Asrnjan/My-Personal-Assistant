"""During the day: meeting notes become delegations, the calendar is protected, and
promises and reminders aren't forgotten."""

from __future__ import annotations

from datetime import timedelta

from app.domain.enums import ItemStatus, Source
from app.engine.reminders import Reminders
from app.integrations.base import CalendarEvent, EmailMessage
from app.llm.fake import fake_message
from app.scheduler import AssistantJobs
from tests.harness import Harness, at_ist, harness, tool_call  # noqa: F401


def _jobs(h: Harness) -> AssistantJobs:
    jobs: AssistantJobs = h.app.state.jobs
    return jobs


def _reminders(h: Harness) -> Reminders:
    found = h.app.state.agent.skills.reminders
    assert found is not None
    return found


def test_meeting_notes_become_a_summary_and_delegations(harness: Harness) -> None:
    notes = "Pricing review. Rahul to send revised margins by Monday. Someone to book an offsite."
    harness.sender.queue(
        tool_call("capture_meeting_notes", notes=notes, meeting="Pricing review"),
        fake_message(
            {
                "summary": "You agreed to revise margins.",
                "decisions": ["Hold prices this quarter."],
                "actions": [
                    {"task": "Send revised margins", "owner": "Rahul", "due": "2026-10-19"},
                    {"task": "Book an offsite", "owner": None, "due": None},
                ],
            }
        ),
    )
    reply = harness.converse(notes)
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "You agreed to revise margins." in result and "Book an offsite" in result
    assert len(reply["pending_actions"]) == 1
    assert "Send revised margins" in reply["reply"] and "Rahul Kumar" in reply["reply"]
    assert harness.item("Book an offsite").source == Source.NOTES

    harness.converse("yes")
    assert harness.item("Send revised margins").status == ItemStatus.DELEGATED
    assert harness.mail.sent[-1].to == ("rahul.kumar@example.com",)


def _busy_morning(h: Harness) -> None:
    h.calendar.events = {
        "m1": CalendarEvent(Source.GCAL, "m1", "Standup", at_ist(9), at_ist(10)),
        "m2": CalendarEvent(Source.GCAL, "m2", "Board call", at_ist(10, 30), at_ist(12)),
        "m3": CalendarEvent(Source.GCAL, "m3", "Pricing review", at_ist(16), at_ist(16, 45)),
    }
    h.sync()


def test_focus_time_goes_in_the_first_free_gap(harness: Harness) -> None:
    _busy_morning(harness)
    harness.sender.queue(tool_call("block_focus_time", date="2026-10-16"))
    reply = harness.converse("Block focus time today")
    assert "Holding 12 pm to 1:30 pm today as focus time. Confirm?" in reply["reply"]
    harness.converse("yes")
    created = harness.calendar.created[-1]
    assert created.title == "Focus time" and created.attendees == () and not created.online
    assert created.start == at_ist(12)


def test_no_room_for_focus_time(harness: Harness) -> None:
    harness.calendar.events = {
        "all": CalendarEvent(Source.GCAL, "all", "Offsite", at_ist(9), at_ist(18))
    }
    harness.sync()
    harness.sender.queue(tool_call("block_focus_time", date="2026-10-16"))
    harness.converse("Block focus time today")
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "no free 90 minutes" in result


def test_move_a_meeting_after_a_yes(harness: Harness) -> None:
    _busy_morning(harness)
    meeting = harness.item("Pricing review")
    harness.sender.queue(
        tool_call("move_meeting", item_ref=meeting.id, date="2026-10-17", start_time="10:00")
    )
    reply = harness.converse("Move the pricing review to tomorrow at 10")
    assert (
        "Moving Pricing review to tomorrow at 10 am; the attendees get the update. Confirm?"
        in reply["reply"]
    )
    harness.converse("yes")
    assert harness.calendar.moved == [("m3", at_ist(10, days=1), at_ist(10, 45, days=1))]
    assert harness.item("Pricing review").start == at_ist(10, days=1)
    assert harness.audit("meeting.move")[0].details["previous"]["start"]


def test_reminders_are_delivered_at_their_time(harness: Harness) -> None:
    harness.sender.queue(
        tool_call("remind_me", text="call the auditor", date="2026-10-16", time="09:30")
    )
    harness.converse("Remind me to call the auditor at 9:30")
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "today at 9:30 am" in result
    assert _jobs(harness).send_reminders() == []
    harness.clock.now += timedelta(minutes=31)
    assert _jobs(harness).send_reminders() == ["call the auditor"]
    assert harness.telegram.ceo_messages[-1] == "Reminder: call the auditor"
    assert _jobs(harness).send_reminders() == []


def test_promises_in_sent_mail_come_back_in_the_morning_brief(harness: Harness) -> None:
    harness.mail.outbox = [
        EmailMessage(
            Source.GMAIL,
            "s-1",
            "t-1",
            "Deck",
            "ceo@example.com",
            recipients=("priya@acme.com",),
            snippet="I'll send you the deck today.",
            received_at=at_ist(8),
        )
    ]
    harness.sender.queue(
        fake_message(
            {
                "promises": [
                    {"message_id": "s-1", "to": "Priya", "what": "send the deck", "due": None},
                    {"message_id": "made-up", "to": "X", "what": "anything", "due": None},
                ]
            }
        )
    )
    assert _reminders(harness).scan_sent() == 1
    assert _reminders(harness).scan_sent() == 0  # each sent email is read once
    brief = _jobs(harness).morning_brief()
    assert "You told Priya you'd send the deck" not in brief.text  # due in two days
    harness.clock.now += timedelta(days=2)
    brief = _jobs(harness).morning_brief()
    assert "You told Priya you'd send the deck today." in brief.text

    harness.sender.queue(tool_call("reminder_done", text="sent the deck to Priya"))
    harness.converse("I've sent the deck to Priya")
    assert "Priya" not in _jobs(harness).morning_brief().text


def test_people_waiting_on_an_answer(harness: Harness) -> None:
    harness.mail.inbox = [
        EmailMessage(
            Source.GMAIL, f"g-{n}", f"t-{n}", f"Budget {n}", "cfo@acme.com", received_at=at_ist(8)
        )
        for n in range(2)
    ]
    harness.sync()
    assert "cfo@acme.com has written 2 times without an answer." in (
        _jobs(harness).morning_brief().text
    )
    harness.sender.queue(tool_call("list_reminders"))
    harness.converse("What am I forgetting?")
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "cfo@acme.com has 2 emails waiting for you" in result
