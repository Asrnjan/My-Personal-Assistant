"""Trust and control: a weekly account of what was done, undo, and permission levels."""

from __future__ import annotations

from app.domain.enums import ActionKind, ApprovedVia, ItemStatus, Source
from app.engine.activity import Activity
from app.integrations.base import CalendarEvent
from tests.harness import Harness, at_ist, harness, say, tool_call  # noqa: F401
from tests.integration.test_delegation_flow import actions, seed


def _activity(h: Harness) -> Activity:
    found = h.app.state.agent.skills.activity
    assert found is not None
    return found


def _approve(h: Harness, kind: ActionKind, title: str, params: dict[str, str]) -> None:
    item = h.item(title)
    action = actions(h).propose(kind, item.id, f"{kind.value} {title}. Confirm?", params)
    assert actions(h).confirm(action.id, ApprovedVia.TAP).ok


def test_weekly_report_lists_and_undoes(harness: Harness) -> None:
    seed(harness)
    _approve(harness, ActionKind.DEFER, "Hiring plan", {"until": "2026-10-20"})
    _approve(harness, ActionKind.DROP, "Vendor contract review", {})
    assert _activity(harness).weekly() == (
        "This week I deferred 1 item and dropped 1 item. "
        "Ask me what I did to hear each one, or say undo."
    )
    harness.sender.queue(tool_call("what_did_you_do"))
    harness.converse("What did you do this week?")
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "Dropped Vendor contract review." in result

    harness.sender.queue(tool_call("undo", what="the hiring plan"))
    harness.converse("Undo the hiring plan")
    assert harness.item("Hiring plan").status == ItemStatus.OPEN
    assert harness.item("Vendor contract review").status == ItemStatus.DROPPED
    assert harness.audit("undo.defer")

    assert _activity(harness).undo("").message == "Vendor contract review is back on your list."


def test_sent_messages_cant_be_unsent_and_moves_go_back_after_a_yes(harness: Harness) -> None:
    harness.calendar.events = {
        "m": CalendarEvent(Source.GCAL, "m", "Pricing review", at_ist(16), at_ist(17))
    }
    harness.sync()
    _approve(
        harness,
        ActionKind.MOVE_MEETING,
        "Pricing review",
        {"start": at_ist(10, days=1).isoformat(), "end": at_ist(11, days=1).isoformat()},
    )
    harness.sender.queue(tool_call("undo", what="pricing review"))
    reply = harness.converse("Put the pricing review back")
    assert "Moving Pricing review back to its old time. Confirm?" in reply["reply"]
    harness.converse("yes")
    assert harness.calendar.moved[-1] == ("m", at_ist(16), at_ist(17))

    action = actions(harness).propose(
        ActionKind.SEND_EMAIL,
        None,
        "x",
        {"to": ["meera.shah@example.com"], "subject": "Hi", "text": "Hello"},
    )
    actions(harness).confirm(action.id, ApprovedVia.TAP)
    assert _activity(harness).undo("the email to Meera").message.startswith("An email can't")
    assert _activity(harness).undo("nothing like this").message == (
        "I couldn't find that among this week's actions."
    )


def test_team_emails_can_go_without_asking_but_outsiders_always_wait(harness: Harness) -> None:
    harness.sender.queue(tool_call("set_permission", action="send_email", mode="team"))
    harness.converse("Send emails to my team without asking")
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "Emails to your team go without asking." in result

    harness.sender.queue(
        tool_call("send_email", to="Meera", subject="Budget", text="Please send it."), say("")
    )
    reply = harness.converse("Email Meera asking for the budget")
    assert reply["pending_actions"] == [] and reply["reply"] == "Sent the email to Meera Shah."
    assert harness.mail.sent[-1].to == ("meera.shah@example.com",)

    # Someone outside the team still gets a read-back.
    harness.sender.queue(
        tool_call("send_email", to="new.person@partner.com", subject="Hi", text="Welcome."),
        say(""),
    )
    outside = harness.converse("Email new.person@partner.com and say welcome")
    assert outside["pending_actions"][0]["kind"] == "send_email"

    harness.sender.queue(tool_call("set_permission", action="send_email", mode="ask"))
    harness.converse("Always ask before emailing")
    harness.sender.queue(
        tool_call("send_email", to="Meera", subject="Budget", text="Again."), say("")
    )
    again = harness.converse("Email Meera again")
    assert again["pending_actions"][0]["kind"] == "send_email"
    harness.sender.queue(tool_call("list_permissions"))
    harness.converse("What can you do without asking?")
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "Emails always wait for your yes." in result


def test_quiet_week_report(harness: Harness) -> None:
    assert _activity(harness).weekly() == "This week I didn't act on anything for you."


def test_a_missing_claude_key_gets_a_spoken_answer_not_a_crash(harness: Harness) -> None:
    from app.llm.errors import LLMAuthError

    def no_key(_params: object) -> None:
        raise LLMAuthError("no Claude API key is set")

    harness.sender.queue(no_key)  # type: ignore[arg-type]
    reply = harness.converse("Think through the Pune office idea")
    assert reply["reply"].startswith("I can't reach Claude: its key is missing or wrong.")
