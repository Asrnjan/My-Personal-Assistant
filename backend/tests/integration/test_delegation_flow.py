from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import DecisionRow, FollowUpRow, ItemRow, LearningSignalRow
from app.domain.enums import (
    ActionKind,
    ApprovedVia,
    FollowUpState,
    ItemKind,
    ItemStatus,
    PriorityBand,
    Source,
)
from app.engine.actions import ActionError, ActionService
from app.engine.approval import ApprovalPolicy
from app.engine.followup import FollowUpService
from app.integrations.base import CalendarEvent, EmailMessage, ExternalTask
from app.integrations.http import IntegrationError
from app.integrations.notify.base import AckEvent
from app.integrations.registry import Adapters
from app.llm.errors import LLMOutputError
from tests.harness import Harness, at_ist, harness  # noqa: F401


def seed(h: Harness) -> None:
    h.calendar.events = {
        "m1": CalendarEvent(Source.GCAL, "m1", "Weekly sync", at_ist(16), at_ist(16, 30)),
    }
    h.tasks.tasks = {
        "OPS-1": ExternalTask(Source.JIRA, "OPS-1", "Vendor contract review", due=at_ist(18)),
        "HR-1": ExternalTask(Source.JIRA, "HR-1", "Hiring plan", due=at_ist(18, days=2)),
    }
    h.sync()


def actions(h: Harness) -> ActionService:
    service: ActionService = h.app.state.actions
    return service


def followups(h: Harness) -> FollowUpService:
    service: FollowUpService = h.app.state.followups
    return service


def delegate(h: Harness, title: str, who: str, due: str | None = None) -> str:
    item = h.item(title)
    action = actions(h).propose(
        ActionKind.DELEGATE,
        item.id,
        "Sending. Confirm?",
        {"delegate_id": who, "due": due, "handoff_type": "keep_me_updated", "note": ""},
    )
    result = actions(h).confirm(action.id, ApprovedVia.TAP)
    assert result.ok, result.message
    with h.sessions() as s:
        decision = s.scalars(select(DecisionRow).where(DecisionRow.item_id == item.id)).one()
        fu = s.scalars(select(FollowUpRow).where(FollowUpRow.decision_id == decision.id)).one()
        return fu.id


def test_one_channel_fails_others_still_send(harness: Harness) -> None:
    seed(harness)
    harness.telegram.fail_with = IntegrationError("telegram", "down")
    item = harness.item("Vendor contract review")
    action = actions(harness).propose(
        ActionKind.DELEGATE, item.id, "x", {"delegate_id": "rahul", "due": None}
    )
    result = actions(harness).confirm(action.id, ApprovedVia.VOICE)
    assert result.ok
    assert result.message == (
        "Done. Rahul Kumar has Vendor contract review by email. "
        "Telegram failed, so I used the other channels."
    )
    assert len(harness.mail.sent) == 1
    outcomes = {(a.action, a.outcome) for a in harness.audit()}
    assert ("delegate.telegram", "failed") in outcomes and ("delegate.email", "ok") in outcomes


def test_all_channels_fail(harness: Harness) -> None:
    seed(harness)
    harness.telegram.fail_with = IntegrationError("telegram", "down")
    harness.mail.fail_with = IntegrationError("gmail", "down")
    item = harness.item("Vendor contract review")
    action = actions(harness).propose(ActionKind.DELEGATE, item.id, "x", {"delegate_id": "rahul"})
    result = actions(harness).confirm(action.id, ApprovedVia.VOICE)
    assert not result.ok
    assert result.message.startswith("I couldn't reach Rahul Kumar")
    assert harness.item("Vendor contract review").status == ItemStatus.OPEN
    # Confirming again just reports the earlier outcome.
    again = actions(harness).confirm(action.id, ApprovedVia.VOICE)
    assert again.ok is False and again.message == result.message


def test_meeting_delegation_and_source_problems(harness: Harness) -> None:
    seed(harness)
    delegate(harness, "Weekly sync", "meera")
    assert harness.calendar.added == [("m1", "meera.shah@example.com")]
    assert harness.calendar.declined == [("m1", "Meera Shah will attend on my behalf.")]
    assert harness.teams.sent and harness.mail.sent

    # Meera has no Jira account id, so the task can't be reassigned to her.
    item = harness.item("Hiring plan")
    action = actions(harness).propose(ActionKind.DELEGATE, item.id, "x", {"delegate_id": "meera"})
    result = actions(harness).confirm(action.id, ApprovedVia.VOICE)
    assert "I couldn't reassign it in Jira (no jira id)." in result.message

    harness.tasks.fail_with = IntegrationError("jira", "boom")
    item = harness.item("Vendor contract review")
    action = actions(harness).propose(ActionKind.DELEGATE, item.id, "x", {"delegate_id": "rahul"})
    harness.mail.fail_with = None
    result = actions(harness).confirm(action.id, ApprovedVia.VOICE)
    assert "Reassigning it in Jira failed." in result.message


def test_unknown_delegate_and_llm_fallback(harness: Harness) -> None:
    seed(harness)
    item = harness.item("Vendor contract review")
    gone = actions(harness).propose(ActionKind.DELEGATE, item.id, "x", {"delegate_id": "nobody"})
    assert actions(harness).confirm(gone.id, ApprovedVia.TAP).message == (
        "That person is no longer in your contacts."
    )

    def broken(_p: dict[str, Any]) -> Any:
        raise LLMOutputError("nope")

    harness.sender.queue(broken)
    ok = actions(harness).propose(ActionKind.DELEGATE, item.id, "x", {"delegate_id": "rahul"})
    assert actions(harness).confirm(ok.id, ApprovedVia.TAP).ok
    assert "please take over Vendor contract review" in harness.mail.sent[-1].text


def test_action_errors_and_failures(harness: Harness) -> None:
    seed(harness)
    task = harness.item("Vendor contract review")
    with pytest.raises(ActionError):
        actions(harness).propose(ActionKind.DROP, "missing", "x")
    with pytest.raises(ActionError):
        actions(harness).confirm("missing", ApprovedVia.TAP)
    with pytest.raises(ActionError):
        actions(harness).cancel("missing")
    not_meeting = actions(harness).propose(ActionKind.DECLINE_MEETING, task.id, "x")
    assert "isn't a meeting" in actions(harness).confirm(not_meeting.id, ApprovedVia.TAP).message

    harness.calendar.fail_with = IntegrationError("gcal", "nope")
    meeting = harness.item("Weekly sync")
    decline = actions(harness).propose(ActionKind.DECLINE_MEETING, meeting.id, "x")
    assert actions(harness).confirm(decline.id, ApprovedVia.TAP).message == (
        "Declining Weekly sync failed."
    )
    harness.tasks.fail_with = IntegrationError("jira", "nope")
    close = actions(harness).propose(ActionKind.CLOSE_TASK, task.id, "x")
    assert actions(harness).confirm(close.id, ApprovedVia.TAP).message == (
        "Closing Vendor contract review failed."
    )
    # A newer proposal replaces an older one for the same item and kind.
    first = actions(harness).propose(ActionKind.DROP, task.id, "x")
    second = actions(harness).propose(ActionKind.DROP, task.id, "y")
    assert [a.id for a in actions(harness).pending()] == [second.id]
    assert actions(harness).get(first.id).status.value == "cancelled"  # type: ignore[union-attr]
    with pytest.raises(ActionError):
        ActionService(
            harness.sessions,
            harness.app.state.delegation,
            ApprovalPolicy(never=["drop"], confirm=["delegate"]),
            at_ist(0).tzinfo,  # type: ignore[arg-type]
            harness.clock,
        ).propose(ActionKind.DROP, task.id, "x")


def test_no_calendar_connected(harness: Harness) -> None:
    seed(harness)
    meeting = harness.item("Weekly sync")
    harness.app.state.delegation._adapters_factory = Adapters  # type: ignore[attr-defined]
    decline = actions(harness).propose(ActionKind.DECLINE_MEETING, meeting.id, "x")
    assert "isn't connected" in actions(harness).confirm(decline.id, ApprovedVia.TAP).message


def test_defer_and_reopen(harness: Harness) -> None:
    seed(harness)
    task = harness.item("Vendor contract review")
    defer = actions(harness).propose(ActionKind.DEFER, task.id, "x", {"until": "2026-10-19"})
    assert actions(harness).confirm(defer.id, ApprovedVia.TAP).ok
    assert harness.item("Vendor contract review").status == ItemStatus.DEFERRED
    assert actions(harness).reopen_deferred() == 0
    harness.clock.now = at_ist(9, days=3)
    harness.sync()
    reopened = harness.item("Vendor contract review")
    assert reopened.status == ItemStatus.OPEN and reopened.deferred_until is None


def test_email_reply_acknowledges(harness: Harness) -> None:
    seed(harness)
    fid = delegate(harness, "Vendor contract review", "rahul", "2026-10-19")
    later = harness.clock.now + timedelta(hours=1)
    harness.mail.threads["thread-1"] = [
        EmailMessage(
            Source.GMAIL,
            "r0",
            "thread-1",
            "Re",
            "someone@else.com",
            snippet="ok",
            received_at=later,
        ),
        EmailMessage(
            Source.GMAIL,
            "r1",
            "thread-1",
            "Re",
            "rahul.kumar@example.com",
            snippet="Accepted, on it.",
            received_at=later,
        ),
    ]
    report = followups(harness).run()
    assert report.acknowledged == 1
    view = followups(harness).views()[0]
    assert view.state == FollowUpState.ACKNOWLEDGED and view.delegate_name == "Rahul Kumar"
    assert view.channels == ["email", "telegram"]
    with harness.sessions() as s:
        assert s.get(FollowUpRow, fid).seen_replies == ["r1"]  # type: ignore[union-attr]
    assert followups(harness).run().acknowledged == 0  # each reply is read once
    assert followups(harness).views(open_only=True) == []


def test_email_question_and_keyword_fallback(harness: Harness) -> None:
    seed(harness)
    delegate(harness, "Vendor contract review", "rahul")
    later = harness.clock.now + timedelta(minutes=5)
    harness.mail.threads["thread-1"] = [
        EmailMessage(
            Source.GMAIL,
            "q1",
            "thread-1",
            "Re",
            "rahul.kumar@example.com",
            snippet="Which vendor?",
            received_at=later,
        ),
    ]

    def broken(_p: dict[str, Any]) -> Any:
        raise LLMOutputError("nope")

    harness.sender.queue(broken)
    report = followups(harness).check_replies()
    assert report.questions == 1
    assert (
        "Rahul Kumar has a question about Vendor contract review."
        in harness.telegram.ceo_messages[-1]
    )
    assert followups(harness).classify_reply("Okay will do").acknowledged is True


def test_telegram_ack_checks_sender_and_slack_polling(harness: Harness) -> None:
    seed(harness)
    fid = delegate(harness, "Vendor contract review", "rahul")
    assert not followups(harness).acknowledge(fid, "telegram", sender_check=lambda d: False)
    assert not followups(harness).acknowledge("missing", "telegram")
    assert not followups(harness).question("missing", "telegram")
    assert not followups(harness).question(fid, "telegram", sender_check=lambda d: False)
    assert followups(harness).acknowledge(
        fid, "telegram", sender_check=lambda d: d.telegram_chat_id == "100000001"
    )
    assert followups(harness).acknowledge(fid, "telegram")  # idempotent

    # Rahul Mehta prefers Slack; acknowledgement arrives by polling.
    with harness.sessions.begin() as s:
        s.add(
            ItemRow(
                id="infra",
                source=Source.JIRA,
                source_id="OPS-9",
                kind=ItemKind.TASK,
                title="Infra upgrade",
                participants=[],
            )
        )
    fid2 = delegate(harness, "Infra upgrade", "rahul-m")
    harness.slack.acks = [AckEvent("acknowledged", "on it", "ts1")]
    assert followups(harness).check_replies().acknowledged == 1
    with harness.sessions() as s:
        assert s.get(FollowUpRow, fid2).state == FollowUpState.ACKNOWLEDGED  # type: ignore[union-attr]


def test_nudge_and_overdue(harness: Harness) -> None:
    seed(harness)
    fid = delegate(harness, "Vendor contract review", "rahul", "2026-10-19")
    assert followups(harness).nudge_due() == 0  # too early
    harness.clock.now = at_ist(19, days=2)  # Sunday evening, deadline Monday 18:00
    assert followups(harness).nudge_due() == 1
    assert harness.telegram.nudges[-1][0] == "rahul"
    assert "reminder" in harness.telegram.nudges[-1][1]
    assert harness.mail.sent[-1].subject.startswith("Reminder: Delegated: Vendor contract review")
    assert followups(harness).nudge_due() == 0  # only once
    harness.clock.now = at_ist(19, days=3)
    assert followups(harness).mark_overdue() == 1
    with harness.sessions() as s:
        assert s.get(FollowUpRow, fid).state == FollowUpState.OVERDUE  # type: ignore[union-attr]
    assert "overdue" in harness.telegram.ceo_messages[-1]


def test_nudge_policy_confirm_tells_ceo_instead(harness: Harness) -> None:
    seed(harness)
    delegate(harness, "Vendor contract review", "rahul", "2026-10-19")
    service = followups(harness)
    service._policy = ApprovalPolicy(  # type: ignore[attr-defined]
        auto=[], confirm=["nudge_delegate", "delegate"]
    )
    harness.clock.now = at_ist(19, days=2)
    assert service.nudge_due() == 0
    assert harness.telegram.nudges == []
    assert (
        "hasn't acknowledged Vendor contract review, due tomorrow."
        in harness.telegram.ceo_messages[-1]
    )
    service._policy = ApprovalPolicy(auto=[], never=["nudge_delegate"])  # type: ignore[attr-defined]
    assert service.nudge_due() == 0


def test_nudge_channel_failure(harness: Harness) -> None:
    seed(harness)
    delegate(harness, "Vendor contract review", "rahul", "2026-10-19")
    harness.clock.now = at_ist(19, days=2)
    harness.telegram.fail_with = IntegrationError("telegram", "down")
    harness.mail.fail_with = IntegrationError("gmail", "down")
    assert followups(harness).nudge_due() == 0
    assert ("nudge.telegram", "failed") in {(a.action, a.outcome) for a in harness.audit()}


def test_learning_records_decline_of_p1(harness: Harness) -> None:
    seed(harness)
    harness.update_item("Weekly sync", priority_band=PriorityBand.P1_DO_MYSELF)
    meeting = harness.item("Weekly sync")
    decline = actions(harness).propose(ActionKind.DECLINE_MEETING, meeting.id, "x")
    actions(harness).confirm(decline.id, ApprovedVia.TAP)
    with harness.sessions() as s:
        signal = s.scalars(select(LearningSignalRow)).one()
    assert signal.rule_text == (
        'The CEO declined "Weekly sync" from P1 (do it myself) to P3 (decline).'
    )
