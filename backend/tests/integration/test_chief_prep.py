"""Before the day: a smarter morning brief, meeting prep and inbox triage."""

from __future__ import annotations

from datetime import timedelta

from app.domain.enums import PriorityBand, Source
from app.engine.insights import DayInsights
from app.integrations.base import Attendee, CalendarEvent, EmailMessage, ExternalTask
from app.scheduler import AssistantJobs
from tests.harness import Harness, at_ist, harness, say, tool_call  # noqa: F401
from tests.integration.test_delegation_flow import delegate


def _jobs(h: Harness) -> AssistantJobs:
    jobs: AssistantJobs = h.app.state.jobs
    return jobs


def test_morning_brief_warns_about_overlaps_focus_time_and_late_team_work(
    harness: Harness,
) -> None:
    harness.calendar.events = {
        f"m{h}": CalendarEvent(Source.GCAL, f"m{h}", f"Meeting {h}", at_ist(h), at_ist(h + 1))
        for h in range(10, 17)
    }
    harness.calendar.events["clash"] = CalendarEvent(
        Source.GCAL, "clash", "Investor call", at_ist(14, 30), at_ist(15)
    )
    harness.tasks.tasks = {
        "OPS-1": ExternalTask(Source.JIRA, "OPS-1", "Vendor contract review", due=at_ist(18))
    }
    harness.sync()
    delegate(harness, "Vendor contract review", "rahul", due="2026-10-15")

    brief = _jobs(harness).morning_brief()
    assert "Heads up: Meeting 14 at 2 pm overlaps with Investor call at 2:30 pm." in brief.text
    assert "no free block for deep work" in brief.text
    assert "1 delegated item is late: Vendor contract review from Rahul Kumar." in brief.text


def test_day_insights_sentences() -> None:
    clear = DayInsights(longest_free_minutes=240)
    assert clear.sentences() == []
    over = DayInsights(longest_free_minutes=None)
    assert not over.no_focus_time
    busy = DayInsights(
        conflicts=[("A at 1 pm", "B at 1 pm")] * 3,
        longest_free_minutes=30,
        team_overdue=["W", "X", "Y", "Z"],
    )
    assert busy.sentences()[2] == "1 more meetings overlap."
    assert busy.sentences()[-1] == "4 delegated items are late: W, X, Y, and 1 more."


def _meeting_soon(h: Harness) -> None:
    start = h.clock() + timedelta(minutes=8)
    h.calendar.events = {
        "m1": CalendarEvent(
            Source.GCAL,
            "m1",
            "Pricing review",
            start,
            start + timedelta(minutes=30),
            attendees=(Attendee("rahul.kumar@example.com", "Rahul"),),
        )
    }
    h.mail.inbox = [
        EmailMessage(
            Source.GMAIL,
            "g-1",
            "t-1",
            "Pricing numbers",
            "rahul.kumar@example.com",
            snippet="Margins are down 3 points.",
            received_at=at_ist(8),
        )
    ]
    h.tasks.tasks = {
        "OPS-1": ExternalTask(Source.JIRA, "OPS-1", "Vendor contract review", due=at_ist(18))
    }
    h.sync()


def test_meeting_prep_is_sent_once_before_the_meeting(harness: Harness) -> None:
    _meeting_soon(harness)
    delegate(harness, "Vendor contract review", "rahul")
    harness.sender.queue(say("Pricing review is with Rahul Kumar. He owes you the vendor review."))

    sent = _jobs(harness).send_preps()
    assert sent == ["Pricing review is with Rahul Kumar. He owes you the vendor review."]
    prompt = harness.sender.calls[-1]["messages"][0]["content"]
    assert "Attendees: Rahul Kumar" in prompt
    assert "Delegated to Rahul Kumar: Vendor contract review" in prompt
    assert "Margins are down 3 points." in prompt and '<untrusted source="recent emails">' in prompt
    assert harness.telegram.ceo_messages[-1].startswith("Before your next meeting: Pricing review")
    # Never twice for the same meeting.
    assert _jobs(harness).send_preps() == []


def test_meeting_prep_on_request_and_offline_fallback(harness: Harness) -> None:
    _meeting_soon(harness)
    # The prep itself comes from the offline responder, after the tool call.
    harness.sender.queue(tool_call("prep_meeting"))
    harness.converse("Prep me for my next meeting")
    tool_result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "Pricing review is with rahul.kumar@example.com" not in tool_result
    assert "Pricing review is with Rahul Kumar" in tool_result


def _inbox(h: Harness) -> None:
    h.mail.inbox = [
        EmailMessage(
            Source.GMAIL,
            f"g-{n}",
            f"t-{n}",
            subject,
            sender,
            snippet="Please reply.",
            received_at=at_ist(8),
            flagged=True,
        )
        for n, (subject, sender) in enumerate(
            [
                ("Board deck", "chair@board.com"),
                ("Office party", "hr@acme.com"),
                ("Newsletter", "news@vendor.com"),
            ]
        )
    ]
    h.sync()
    h.update_item("Board deck", priority_band=PriorityBand.P1_DO_MYSELF, priority_score=95)
    h.update_item("Office party", priority_band=PriorityBand.P2_DELEGATE, priority_score=50)
    h.update_item("Newsletter", priority_band=PriorityBand.P4_DROP, priority_score=5)


def test_triage_sorts_the_inbox_and_drafts_replies_to_approve(harness: Harness) -> None:
    _inbox(harness)
    harness.sender.queue(tool_call("triage_inbox"))
    reply = harness.converse("Triage my inbox")
    readback = (
        "Replying to chair@board.com about Re: Board deck, saying: "
        "Thanks, I'll come back to you on this. Confirm?"
    )
    assert readback in reply["reply"]
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "1 need you, 1 can be delegated, 1 are just for your information" in result
    assert [a["kind"] for a in reply["pending_actions"]] == ["reply_email"]
    assert harness.mail.sent == []

    harness.converse("yes")
    sent = harness.mail.sent[-1]
    assert sent.to == ("chair@board.com",) and sent.reply_to_message_id == "g-0"
    assert sent.text == "Thanks, I'll come back to you on this."


def test_triage_with_nothing_to_do(harness: Harness) -> None:
    triage = harness.app.state.agent.skills.triage
    assert triage.run().summary() == "Your inbox is clear: no emails need action."
