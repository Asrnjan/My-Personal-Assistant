from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import DEFAULT_CONTACTS_PATH, AppConfig
from app.db.models import DecisionRow, FollowUpRow, ItemRow
from app.domain.enums import (
    Actor,
    ApprovedVia,
    DecisionAction,
    FollowUpState,
    ItemKind,
    ItemStatus,
    PriorityBand,
    Source,
)
from app.engine.briefing import (
    Briefer,
    BriefKind,
    clean_title,
    fingerprint,
    spoken_list,
    spoken_time,
)
from app.engine.contacts import load_contacts, seed_delegates
from tests.unit.llm.conftest import IST, MutableClock

NOW = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)  # 09:00 IST, Friday


def at_ist(hour: int, minute: int = 0, days: int = 0) -> datetime:
    return datetime(2026, 10, 16 + days, hour, minute, tzinfo=IST)


_counter = iter(range(10_000))


def add(sessions: sessionmaker[Session], **kw: Any) -> str:
    n = next(_counter)
    row: dict[str, Any] = {
        "id": f"i{n}",
        "source": Source.JIRA,
        "source_id": f"X-{n}",
        "kind": ItemKind.TASK,
        "title": f"Task {n}",
        "participants": [],
        "status": ItemStatus.OPEN,
    }
    row.update(kw)
    with sessions.begin() as s:
        s.add(ItemRow(**row))
    return str(row["id"])


def meeting(sessions: sessionmaker[Session], title: str, start: datetime, **kw: Any) -> str:
    return add(
        sessions,
        source=Source.GCAL,
        kind=ItemKind.MEETING,
        title=title,
        start=start,
        end=start + timedelta(minutes=30),
        **kw,
    )


def scored(band: PriorityBand, score: int, reason: str = "Because.", **kw: Any) -> dict[str, Any]:
    return {"priority_band": band, "priority_score": score, "reason": reason, **kw}


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(NOW)


@pytest.fixture
def briefer(session_factory: sessionmaker[Session], clock: MutableClock) -> Briefer:
    seed_delegates(session_factory, load_contacts(DEFAULT_CONTACTS_PATH))
    return Briefer(session_factory, AppConfig(), clock)


def test_spoken_helpers() -> None:
    assert spoken_time(at_ist(10), IST) == "10 am"
    assert spoken_time(at_ist(16, 30), IST) == "4:30 pm"
    assert spoken_time(at_ist(12), IST) == "12 pm"
    assert spoken_time(at_ist(0, 5), IST) == "12:05 am"
    assert spoken_list([]) == ""
    assert spoken_list(["a"]) == "a"
    assert spoken_list(["a", "b"]) == "a and b"
    assert spoken_list(["a", "b", "c"]) == "a, b, and c"
    assert clean_title("**Board** pack: [review]!") == "Board pack: review"
    assert clean_title("one two three four five six seven eight nine ten") == (
        "one two three four five six seven eight"
    )
    assert clean_title("***") == "an untitled item"


def test_morning_brief(briefer: Briefer, session_factory: sessionmaker[Session]) -> None:
    meeting(session_factory, "Board prep", at_ist(10), **scored(PriorityBand.P1_DO_MYSELF, 95))
    meeting(session_factory, "Weekly sync", at_ist(16), **scored(PriorityBand.P3_DECLINE, 20))
    add(
        session_factory,
        title="Vendor review",
        due=at_ist(18),
        **scored(PriorityBand.P2_DELEGATE, 60, suggested_delegate_id="rahul"),
    )
    add(session_factory, title="Pricing deck", **scored(PriorityBand.P1_DO_MYSELF, 80))
    add(
        session_factory,
        title="Old cleanup",
        due=at_ist(9, days=-3),
        **scored(PriorityBand.P4_DROP, 5),
    )
    add(
        session_factory,
        source=Source.GMAIL,
        source_id="mail:1",
        kind=ItemKind.EMAIL_ACTION,
        title="Reply to investor",
    )
    # Not today's: tomorrow's meeting, a later task, an undated low task, a done task.
    meeting(session_factory, "Offsite", at_ist(9, days=1), **scored(PriorityBand.P1_DO_MYSELF, 90))
    add(session_factory, title="Next week", due=at_ist(9, days=5))
    add(session_factory, title="Backlog", **scored(PriorityBand.P3_DECLINE, 10))
    add(session_factory, title="Finished", due=at_ist(9), status=ItemStatus.DONE)

    brief = briefer.morning()
    assert brief.kind == BriefKind.MORNING and brief.for_date == date(2026, 10, 16)
    assert brief.text == (
        "Good morning. You have 2 meetings, 3 tasks, and 1 email to act on today. "
        "2 need you: Board prep at 10 am and Pricing deck. "
        "I suggest delegating Vendor review to Rahul Kumar. "
        "I suggest declining Weekly sync at 4 pm. "
        "You can drop Old cleanup. "
        "Not yet scored: Reply to investor. "
        "Shall I go ahead?"
    )
    assert [i.suggested for i in brief.items] == ["do", "do", "delegate", "decline", "drop", "open"]
    assert brief.items[2].delegate_id == "rahul" and brief.items[2].delegate_name == "Rahul Kumar"
    assert brief.details[0] == "Board prep at 10 am. Because."
    assert brief.details[2] == "Vendor review, due today. Because. I suggest Rahul Kumar."
    assert brief.details[4] == "Old cleanup, overdue. Because. I suggest dropping it."
    assert brief.details[5] == "Reply to investor. It has not been scored yet."
    assert brief.word_count == len(brief.text.split())
    assert brief.estimated_seconds == round(brief.word_count * 60 / 150)


def test_first_names_used_when_unique(
    briefer: Briefer, session_factory: sessionmaker[Session]
) -> None:
    add(
        session_factory,
        title="Invoice batch",
        due=at_ist(17),
        **scored(PriorityBand.P2_DELEGATE, 50, suggested_delegate_id="meera"),
    )
    brief = briefer.morning()
    assert "I suggest delegating Invoice batch to Meera." in brief.text
    assert "Nothing needs you personally." in brief.text


def test_clear_day_and_other_day(briefer: Briefer) -> None:
    assert briefer.morning().text == "Good morning. Your day is clear today."
    monday = briefer.morning(date(2026, 10, 19))
    assert monday.text == "Good morning. Your day is clear on Monday."
    assert monday.items == [] and monday.details == []


def test_finished_meetings_drop_out(
    briefer: Briefer, session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    meeting(session_factory, "Standup", at_ist(9, 30), **scored(PriorityBand.P1_DO_MYSELF, 70))
    assert "Standup" in briefer.morning().text
    clock.now = at_ist(11)
    assert "Standup" not in briefer.morning().text


def test_long_brief_is_trimmed(briefer: Briefer, session_factory: sessionmaker[Session]) -> None:
    for n in range(12):
        add(
            session_factory,
            title=f"Important customer escalation number {n}",
            due=at_ist(17),
            **scored(PriorityBand.P1_DO_MYSELF, 90 - n),
        )
    for n in range(8):
        add(
            session_factory,
            title=f"Routine approval request {n}",
            due=at_ist(17),
            **scored(PriorityBand.P2_DELEGATE, 50 - n, suggested_delegate_id="meera"),
        )
    brief = briefer.morning()
    assert brief.word_count <= AppConfig().briefing.max_words
    assert brief.estimated_seconds <= 45
    assert "12 need you: Important customer escalation number 0" in brief.text
    assert "more." in brief.text
    # Drill-down still has every item.
    assert len(brief.details) == 20


def test_very_tight_budget_falls_back_to_counts(
    session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    config = AppConfig.model_validate({"briefing": {"max_spoken_seconds": 10}})
    briefer = Briefer(session_factory, config, clock)
    add(session_factory, title="Alpha", due=at_ist(17), **scored(PriorityBand.P1_DO_MYSELF, 90))
    add(session_factory, title="Beta", due=at_ist(17), **scored(PriorityBand.P2_DELEGATE, 50))
    add(session_factory, title="Gamma", due=at_ist(17), **scored(PriorityBand.P3_DECLINE, 20))
    add(session_factory, title="Delta", due=at_ist(17), **scored(PriorityBand.P4_DROP, 5))
    add(session_factory, title="Eps", due=at_ist(17))
    text = briefer.morning().text
    assert "1 needs you." in text
    assert "I suggest delegating 1 item." in text
    assert "I suggest declining 1 item." in text
    assert "You can drop 1 item." in text
    assert "1 item is not scored yet." in text


def _delegation(
    sessions: sessionmaker[Session],
    title: str,
    delegate: str,
    state: FollowUpState,
    acknowledged_at: datetime | None = None,
    due: datetime | None = None,
) -> None:
    item_id = add(sessions, title=title, status=ItemStatus.DELEGATED)
    with sessions.begin() as s:
        dec = DecisionRow(
            item_id=item_id,
            action=DecisionAction.DELEGATE,
            actor=Actor.CEO,
            delegate_id=delegate,
            approved_via=ApprovedVia.VOICE,
        )
        s.add(dec)
        s.flush()
        s.add(
            FollowUpRow(decision_id=dec.id, state=state, acknowledged_at=acknowledged_at, due=due)
        )


def test_evening_wrapup(
    briefer: Briefer, session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    clock.now = at_ist(18, 30)
    add(
        session_factory,
        title="Pricing deck",
        due=at_ist(17),
        **scored(PriorityBand.P1_DO_MYSELF, 80),
    )
    add(session_factory, title="Ignore me", due=at_ist(17), **scored(PriorityBand.P4_DROP, 5))
    add(
        session_factory,
        source=Source.GMAIL,
        source_id="mail:2",
        kind=ItemKind.EMAIL_ACTION,
        title="Reply to investor",
        **scored(PriorityBand.P1_DO_MYSELF, 70),
    )
    meeting(session_factory, "Dinner with client", at_ist(20))
    meeting(session_factory, "Morning standup", at_ist(14))  # already happened
    meeting(session_factory, "Breakfast", at_ist(9, days=1))
    meeting(session_factory, "Later tomorrow", at_ist(15, days=1))
    _delegation(
        session_factory, "Vendor review", "rahul", FollowUpState.PENDING, due=at_ist(9, days=3)
    )
    _delegation(session_factory, "Hiring plan", "arjun", FollowUpState.NUDGED)
    _delegation(session_factory, "Invoice batch", "meera", FollowUpState.ACKNOWLEDGED, at_ist(11))
    _delegation(
        session_factory,
        "Old thing",
        "meera",
        FollowUpState.ACKNOWLEDGED,
        at_ist(11, days=-1),
    )
    _delegation(session_factory, "Closed", "meera", FollowUpState.CLOSED)

    brief = briefer.evening()
    assert brief.kind == BriefKind.EVENING
    assert brief.text == (
        "Good evening. Still ahead tonight: Dinner with client at 8 pm. "
        "2 items are still open: Pricing deck and Reply to investor. "
        "Still waiting to hear back on Vendor review from Rahul Kumar and Hiring plan from Arjun. "
        "Meera accepted Invoice batch. "
        "Shall I move Pricing deck to tomorrow? Tomorrow starts with Breakfast at 9 am."
    )
    assert [i.suggested for i in brief.items] == ["open", "rollover", "open"]
    assert [d.state for d in brief.delegations] == [
        FollowUpState.PENDING,
        FollowUpState.NUDGED,
        FollowUpState.ACKNOWLEDGED,
    ]
    assert "Vendor review: waiting on Rahul Kumar, due on Monday." in brief.details
    assert "Hiring plan: waiting on Arjun." in brief.details
    assert "Pricing deck, due today. Because. I suggest moving it to tomorrow." in brief.details


def test_evening_all_handled_and_rollover_phrasing(
    briefer: Briefer, session_factory: sessionmaker[Session], clock: MutableClock
) -> None:
    clock.now = at_ist(19)
    assert briefer.evening().text == "Good evening. Everything on today's list is handled."
    add(session_factory, title="Contract", due=at_ist(12))
    assert briefer.evening().text.endswith("Shall I move it to tomorrow?")
    add(session_factory, title="Deck", due=at_ist(9, days=-1))
    assert briefer.evening().text.endswith("Shall I move them to tomorrow?")


def test_alerts_new_changed_and_quiet(
    briefer: Briefer, session_factory: sessionmaker[Session]
) -> None:
    assert briefer.alerts() is None
    item_id = add(
        session_factory,
        title="Board letter",
        reason="The board asked.",
        priority_band=PriorityBand.P1_DO_MYSELF,
        priority_score=95,
    )
    add(session_factory, title="Low thing", **scored(PriorityBand.P3_DECLINE, 10))
    first = briefer.alerts()
    assert first is not None and first.kind == BriefKind.ALERT
    assert first.text == "Heads up. New top priority: Board letter. The board asked."
    assert [i.id for i in first.items] == [item_id]
    assert briefer.alerts() is None  # nothing changed

    with session_factory.begin() as s:
        row = s.get(ItemRow, item_id)
        assert row is not None
        row.due = at_ist(17)
    changed = briefer.alerts()
    assert changed is not None
    assert changed.text.startswith("Heads up. Update on a top priority: Board letter.")


def test_alerts_batch_and_skip_past_meetings(
    briefer: Briefer, session_factory: sessionmaker[Session]
) -> None:
    meeting(session_factory, "Over already", at_ist(7), **scored(PriorityBand.P1_DO_MYSELF, 90))
    a = add(session_factory, title="Alpha", **scored(PriorityBand.P1_DO_MYSELF, 90))
    add(session_factory, title="Beta", **scored(PriorityBand.P1_DO_MYSELF, 80))
    first = briefer.alerts()
    assert first is not None
    assert first.text == "Heads up. 2 new top priorities: Alpha and Beta."

    with session_factory.begin() as s:
        row = s.get(ItemRow, a)
        assert row is not None
        row.title = "Alpha v2"
    add(session_factory, title="Gamma", **scored(PriorityBand.P1_DO_MYSELF, 70))
    second = briefer.alerts()
    assert second is not None
    assert second.text == "Heads up. A new top priority: Gamma. Changed: Alpha v2."


def test_mark_alerted_only_touches_p1(
    briefer: Briefer, session_factory: sessionmaker[Session]
) -> None:
    p1 = add(session_factory, title="Alpha", **scored(PriorityBand.P1_DO_MYSELF, 90))
    p2 = add(session_factory, title="Beta", **scored(PriorityBand.P2_DELEGATE, 50))
    briefer.mark_alerted([])
    briefer.mark_alerted([p1, p2])
    with session_factory() as s:
        rows = {r.id: r for r in s.scalars(select(ItemRow))}
        assert rows[p1].alerted_hash == fingerprint(rows[p1])
        assert rows[p2].alerted_hash is None
    assert briefer.alerts() is None
