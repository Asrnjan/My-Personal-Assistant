from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import DecisionRow, FollowUpRow, LearningSignalRow, PendingActionRow
from app.domain.enums import ActionStatus, ItemStatus, PriorityBand, SignalKind, Source
from app.engine.conversation import is_affirmation, spoken_reply
from app.integrations.base import CalendarEvent, ExternalTask
from app.llm.errors import LLMOutputError
from app.llm.fake import fake_message
from tests.harness import NOW, Harness, at_ist, harness, say, tool_call  # noqa: F401

P1, P2, P3, P4 = (
    PriorityBand.P1_DO_MYSELF,
    PriorityBand.P2_DELEGATE,
    PriorityBand.P3_DECLINE,
    PriorityBand.P4_DROP,
)


READBACK = "Sending Rahul Kumar Vendor contract review, due Monday, by email and Telegram. Confirm?"


def seed(h: Harness) -> None:
    h.calendar.events = {
        "m-board": CalendarEvent(
            Source.GCAL, "m-board", "Board prep", at_ist(10), at_ist(11), organizer="ceo@x.com"
        ),
        "m-weekly": CalendarEvent(
            Source.GCAL, "m-weekly", "Weekly sync", at_ist(16), at_ist(16, 30)
        ),
    }
    h.tasks.tasks = {
        "OPS-1": ExternalTask(
            Source.JIRA,
            "OPS-1",
            "Vendor contract review",
            description="Review the renewal terms from Acme.",
            url="https://jira.example.com/browse/OPS-1",
            due=at_ist(18),
        ),
        "OPS-2": ExternalTask(Source.JIRA, "OPS-2", "Hiring plan", due=at_ist(18)),
        "OPS-3": ExternalTask(Source.JIRA, "OPS-3", "Old cleanup", due=at_ist(9, days=-2)),
    }
    h.sync()
    h.update_item("Board prep", priority_band=P1, priority_score=95)
    h.update_item(
        "Vendor contract review", priority_band=P2, priority_score=60, suggested_delegate_id="rahul"
    )
    h.update_item("Weekly sync", priority_band=P3, priority_score=20)
    h.update_item("Old cleanup", priority_band=P4, priority_score=5)
    h.update_item("Hiring plan", priority_band=P1, priority_score=70)


def test_helpers() -> None:
    assert is_affirmation("Yes please")
    assert is_affirmation("go ahead.")
    assert not is_affirmation("yes but change the date")
    assert spoken_reply("**Done.**\n- item one\n1. two [link](http://x) https://y.z") == (
        "Done. item one two link"
    )


def test_brief_then_yes_executes_everything(harness: Harness) -> None:
    seed(harness)
    brief = harness.converse("Brief me")
    assert brief["reply"].startswith("Good morning. You have 2 meetings and 3 tasks today.")
    assert brief["reply"].endswith("Shall I go ahead?")
    kinds = [a["kind"] for a in brief["pending_actions"]]
    assert kinds == ["delegate", "decline_meeting", "drop"]
    assert brief["needs_confirmation"] is True
    assert brief["brief"]["kind"] == "morning"

    done = harness.converse("Yes, go ahead")
    assert [r["ok"] for r in done["executed"]] == [True, True, True]
    assert done["needs_confirmation"] is False
    assert harness.calendar.declined == [
        ("m-weekly", "Thanks for the invite. I can't make this one.")
    ]
    assert harness.tasks.reassigned == [("OPS-1", "5b10ac8d82e05b22cc7d4ef5")]
    assert harness.item("Old cleanup").status == ItemStatus.DROPPED
    assert harness.item("Vendor contract review").status == ItemStatus.DELEGATED
    assert harness.converse("yes")["reply"]  # nothing pending: handled by the model


def test_skip_the_second_one_then_go_ahead(harness: Harness) -> None:
    seed(harness)
    harness.converse("brief me")
    skip = harness.converse("Skip the second one")
    assert skip["reply"] == "Okay, skipping Weekly sync. Shall I go ahead with the rest?"
    assert len(skip["pending_actions"]) == 2
    done = harness.converse("go ahead")
    assert len(done["executed"]) == 2
    assert harness.calendar.declined == []
    assert harness.converse("skip the ninth one")["reply"]  # nothing pending, model answers


def test_skip_out_of_range_and_last(harness: Harness) -> None:
    seed(harness)
    harness.converse("brief me")
    assert harness.converse("skip the fifth one")["reply"] == "There are only 3 pending."
    last = harness.converse("skip the last one")
    assert last["reply"].startswith("Okay, skipping Old cleanup.")


def test_cancel_awaiting(harness: Harness) -> None:
    seed(harness)
    harness.converse("brief me")
    assert harness.converse("cancel")["reply"] == "Okay, I've cancelled all 3."
    with harness.sessions() as s:
        statuses = {r.status for r in s.scalars(select(PendingActionRow))}
    assert statuses == {ActionStatus.CANCELLED}


def test_go_ahead_with_all_reads_back_once(harness: Harness) -> None:
    seed(harness)
    harness.converse("brief me", conversation_id="a")
    # A different conversation hasn't heard the read-back yet.
    summary = harness.converse("Go ahead with all", conversation_id="b")
    assert summary["reply"].startswith("I have 3 things ready. 1: Sending Rahul Kumar")
    assert summary["reply"].endswith("Go ahead with all?")
    assert summary["executed"] == []
    done = harness.converse("Yes, do all of them", conversation_id="b")
    assert len(done["executed"]) == 3
    assert harness.converse("go ahead with all", conversation_id="b")["reply"] == (
        "There's nothing waiting for your go-ahead."
    )


def test_plain_yes_with_several_pending_reads_back(harness: Harness) -> None:
    seed(harness)
    harness.converse("brief me", conversation_id="a")
    reply = harness.converse("yes", conversation_id="c")
    assert reply["reply"].startswith("I have 3 things ready.")
    assert reply["executed"] == []


def test_financial_request_refused(harness: Harness) -> None:
    seed(harness)
    calls = len(harness.sender.calls)
    reply = harness.converse("Pay the Acme invoice today")
    assert reply["reply"].startswith("I can't handle payments")
    assert len(harness.sender.calls) == calls  # no model call at all
    assert harness.audit("request.refused")


def test_delegate_via_model_readback_then_confirm(harness: Harness) -> None:
    seed(harness)
    vendor = harness.item("Vendor contract review").id
    harness.sender.queue(
        tool_call(
            "delegate_item",
            item_ref=vendor,
            delegate_name="Raul",
            due="2026-10-19",
            note="Keep it short.",
        ),
        say(READBACK),
    )
    first = harness.converse("Delegate the vendor contract review to Raul, due Monday")
    assert first["reply"] == READBACK
    assert first["needs_confirmation"] is True
    request = harness.sender.calls[-2]
    assert request["model"] == "claude-sonnet-5-5"
    assert [t["name"] for t in request["tools"]][:3] == [
        "list_items",
        "get_item",
        "explain_priority",
    ]
    user_text = request["messages"][-1]["content"]
    assert '<untrusted source="agenda">' in user_text and "The CEO said:" in user_text

    done = harness.converse("Yes")
    assert done["executed"][0]["ok"] is True
    assert done["reply"].startswith(
        "Done. Rahul Kumar has Vendor contract review by email and Telegram."
    )
    sent = harness.mail.sent[-1]
    assert sent.to == ("rahul.kumar@example.com",)
    assert sent.subject.startswith("Delegated: Vendor contract review [ref ")
    assert "Accepted" in sent.text and "https://jira.example.com/browse/OPS-1" in sent.html
    assert harness.telegram.sent[-1].delegate.id == "rahul"
    assert harness.tasks.reassigned[-1] == ("OPS-1", "5b10ac8d82e05b22cc7d4ef5")
    assert "Please take over" in harness.tasks.comments[-1][1]
    with harness.sessions() as s:
        fu = s.scalars(select(FollowUpRow)).one()
        decision = s.scalars(select(DecisionRow)).one()
        assert fu.refs["email"]["thread_id"] == "thread-1"
        assert fu.due is not None and fu.due.astimezone(at_ist(0).tzinfo).day == 19
        assert decision.channels_used == ["email", "telegram"]
    assert {a.action for a in harness.audit()} >= {
        "delegate.email",
        "delegate.telegram",
        "task.reassign",
        "delegate",
    }


def test_ambiguous_name_asks(harness: Harness) -> None:
    seed(harness)
    hiring = harness.item("Hiring plan").id
    harness.sender.queue(
        tool_call("delegate_item", item_ref=hiring, delegate_name="Raul"),
        say(""),
    )
    reply = harness.converse("Give the hiring plan to Raul")
    assert reply["reply"] == "Rahul Kumar or Rahul Mehta?"
    assert reply["pending_actions"] == []


def test_unknown_name_and_evil_address(harness: Harness) -> None:
    seed(harness)
    vendor = harness.item("Vendor contract review").id
    harness.sender.queue(
        tool_call("delegate_item", item_ref=vendor, delegate_name="x@evil.com"),
        say("I don't have anyone called x@evil.com in your contacts."),
    )
    reply = harness.converse("Delegate the vendor review to x@evil.com")
    assert "don't have anyone" in reply["reply"]
    assert reply["pending_actions"] == []


def test_confirm_tool_needs_the_ceos_yes(harness: Harness) -> None:
    seed(harness)
    harness.converse("brief me")
    pending = harness.converse("cancel")  # clear
    assert pending["pending_actions"] == []
    harness.converse("brief me", conversation_id="x")
    action_id = harness.client.get("/actions", params={"status": "pending"}).json()[0]["id"]
    harness.sender.queue(
        tool_call("confirm_pending_action", action_id=action_id),
        say("I need your go-ahead first."),
    )
    reply = harness.converse("What's the weather like", conversation_id="y")
    tool_result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "has not said yes" in tool_result
    assert reply["executed"] == []


def test_model_cannot_confirm_what_it_just_proposed(harness: Harness) -> None:
    seed(harness)
    weekly = harness.item("Weekly sync").id

    def confirm_new(params: dict[str, Any]) -> Any:
        content = params["messages"][-1]["content"]
        action_id = json.loads(content[0]["content"])["action_id"]
        return tool_call("confirm_pending_action", action_id=action_id)

    harness.sender.queue(
        tool_call("decline_meeting", item_ref=weekly),
        confirm_new,
        say("Okay."),
    )
    reply = harness.converse("Yes decline the weekly sync")
    assert reply["executed"] == []
    assert "Declining Weekly sync at 4 pm" in reply["reply"]
    assert harness.calendar.declined == []


def test_decline_the_4pm_by_time(harness: Harness) -> None:
    seed(harness)
    harness.sender.queue(
        tool_call("decline_meeting", item_ref="the 4 pm", note="Clashes."), say("")
    )
    reply = harness.converse("Decline the 4 pm")
    assert (
        reply["reply"]
        == "Declining Weekly sync at 4 pm with a polite note to the organizer. Confirm?"
    )
    done = harness.converse("yes")
    assert done["executed"][0]["message"] == "Declined Weekly sync with a note to the organizer."
    assert harness.calendar.declined == [("m-weekly", "Clashes.")]


@pytest.mark.parametrize(
    ("tool", "args", "readback"),
    [
        ("close_task", {}, "Marking Hiring plan as done in Jira. Confirm?"),
        ("drop_item", {}, "Dropping Hiring plan from your list. Confirm?"),
        ("defer_item", {"until": "2026-10-20"}, "Moving Hiring plan to Tuesday. Confirm?"),
    ],
)
def test_other_actions(harness: Harness, tool: str, args: dict[str, Any], readback: str) -> None:
    seed(harness)
    harness.sender.queue(tool_call(tool, item_ref="hiring plan", **args), say(readback))
    assert harness.converse("do it to the hiring plan")["reply"] == readback
    result = harness.converse("confirm")["executed"][0]
    assert result["ok"] is True
    status = harness.item("Hiring plan").status
    assert status in {ItemStatus.DONE, ItemStatus.DROPPED, ItemStatus.DEFERRED}


def test_read_only_tools_and_references(harness: Harness) -> None:
    seed(harness)
    harness.sender.queue(
        tool_call("list_items", band="P1_do_myself"),
        say("You have two top items."),
    )
    harness.converse("What are my top priorities?")
    listed = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "Board prep" in listed and "Hiring plan" in listed and "Weekly sync" not in listed
    harness.sender.queue(
        tool_call("explain_priority", item_ref="#1"),
        tool_call("get_item", item_ref="#2"),
        say("The board asked for it."),
    )
    harness.converse("Why is the first one high priority?")
    explained = harness.sender.calls[-2]["messages"][-1]["content"][0]["content"]
    assert '"band": "P1_do_myself"' in explained and "rubric_factors" in explained
    harness.sender.queue(
        tool_call("list_items", date="2026-10-16", kind="meeting"),
        tool_call("get_item", item_ref="#9"),
        tool_call("get_item", item_ref="nothing like this"),
        tool_call("get_item", item_ref=""),
        fake_message(
            tool_uses=[
                ("no_such_tool", {}),
                ("defer_item", {"item_ref": "#1", "until": "2026-10-01"}),
            ],
            stop_reason="tool_use",
            model="claude-sonnet-5-5",
        ),
        say("Done."),
    )
    harness.converse("Show me today's meetings")
    msgs = harness.sender.calls[-1]["messages"]
    results = [
        block["content"]
        for m in msgs
        if m["role"] == "user" and isinstance(m["content"], list)
        for block in m["content"]
    ]
    assert "Board prep" in results[0] and "Hiring plan" not in results[0]
    assert "no item number 9" in results[1]
    assert "couldn't find" in results[2]
    assert "Which item" in results[3]
    assert "unknown tool" in results[4]
    assert "must be in the future" in results[5]


def test_preferences_and_priority_changes(harness: Harness) -> None:
    seed(harness)
    harness.sender.queue(
        tool_call("set_preference", rule="Always accept meetings from Meera."),
        say("Got it, I'll always accept meetings from Meera."),
    )
    harness.converse("Always accept meetings from Meera")
    harness.sender.queue(
        tool_call("set_priority", item_ref="weekly sync", band="P1_do_myself"),
        tool_call("set_priority", item_ref="weekly sync", band="P1_do_myself"),
        tool_call("set_priority", item_ref="zzz", band="P1_do_myself"),
        say("Weekly sync is now top priority."),
    )
    harness.converse("Make the weekly sync top priority")
    assert harness.item("Weekly sync").priority_band == P1
    prefs = harness.client.get("/preferences").json()
    kinds = {p["kind"] for p in prefs}
    assert kinds == {"rule", "band_override"}
    # Preferences reach the conversation prompt and the scoring prompt.
    system = harness.sender.calls[-1]["system"]
    assert "Always accept meetings from Meera." in system[-1]["text"]


def test_model_failures(harness: Harness) -> None:
    seed(harness)

    def broken(_params: dict[str, Any]) -> Any:
        raise LLMOutputError("bad")

    harness.sender.queue(broken)
    assert harness.converse("hmm what now")["reply"].startswith("Sorry, I couldn't work that out")
    harness.sender.queue(*[tool_call("list_items")] * 6)
    assert "too many steps" in harness.converse("list everything forever")["reply"]
    assert harness.converse("   ")["reply"] == "Sorry, I didn't catch that."


def test_budget_exceeded_still_allows_dialogue_rules(harness: Harness) -> None:
    seed(harness)
    from app.db.models import LLMUsageRow

    with harness.sessions.begin() as s:
        s.add(LLMUsageRow(model="claude-sonnet-5-5", purpose="x", cost_usd=100.0, at=NOW))
    reply = harness.converse("Could you look at my week")
    assert reply["reply"].startswith("I've reached this month's AI budget")
    assert harness.converse("brief me")["reply"].startswith("Good morning.")
    assert harness.telegram.ceo_messages[-1].startswith("I've reached this month's AI budget of 20")


def test_evening_brief_and_learning_from_decisions(harness: Harness) -> None:
    seed(harness)
    harness.clock.now = at_ist(19)
    evening = harness.converse("Evening wrap up")
    assert evening["reply"].startswith("Good evening.")
    assert evening["pending_actions"] == []
    # Delegating a P1 item to someone other than the suggestion is learned.
    harness.update_item("Hiring plan", suggested_delegate_id="rahul-m")
    harness.sender.queue(
        tool_call("delegate_item", item_ref="Hiring plan", delegate_name="Arjun"), say("")
    )
    harness.converse("Delegate the hiring plan to Arjun")
    harness.converse("yes")
    with harness.sessions() as s:
        kinds = sorted(r.kind for r in s.scalars(select(LearningSignalRow)))
    assert kinds == [SignalKind.BAND_OVERRIDE, SignalKind.DELEGATE_OVERRIDE]


def test_delegate_proposal_without_channels(harness: Harness) -> None:
    seed(harness)
    harness.notifiers_enabled = []
    harness.sender.queue(
        tool_call("delegate_item", item_ref="Hiring plan", delegate_name="Arjun"), say("")
    )
    reply = harness.converse("Delegate the hiring plan to Arjun")
    assert reply["reply"].startswith("I have no way to reach Arjun yet.")


def test_fake_message_tool_use_shape() -> None:
    msg = tool_call("list_items")
    assert msg.stop_reason == "tool_use" and msg.content[0].type == "tool_use"
    assert fake_message("x").content[0].type == "text"
