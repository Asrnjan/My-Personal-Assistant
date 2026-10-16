from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import AppConfig
from app.db.models import ItemRow
from app.domain.enums import ItemKind, ItemStatus, Source
from app.engine.ingest import Ingestor, normalize_title
from app.integrations.base import Attendee, CalendarEvent, EmailMessage, ExternalTask
from app.integrations.fake import FakeCalendar, FakeMail, FakeTasks
from app.integrations.http import IntegrationError
from app.integrations.registry import Adapters, SyncStateStore

NOW = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)  # 09:00 in India
CEO = Attendee("ceo@example.com", is_self=True, response="needsAction")


def meeting(id_: str, hours: float, *, source: Source = Source.GCAL, **kw: object) -> CalendarEvent:
    start = NOW + timedelta(hours=hours)
    base = CalendarEvent(
        source=source,
        id=id_,
        title=f"Meeting {id_}",
        start=start,
        end=start + timedelta(minutes=30),
        organizer="cfo@example.com",
        attendees=(CEO, Attendee("cfo@example.com")),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def task(id_: str, **kw: object) -> ExternalTask:
    return replace(  # type: ignore[arg-type]
        ExternalTask(source=Source.JIRA, id=id_, title=f"Task {id_}", reporter="Meera@Example.com"),
        **kw,
    )


def email(id_: str, subject: str, sender: str = "meera@example.com") -> EmailMessage:
    return EmailMessage(
        source=Source.GMAIL,
        id=id_,
        thread_id=f"t-{id_}",
        subject=subject,
        sender=sender,
        recipients=("ceo@example.com",),
        received_at=NOW - timedelta(hours=1),
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def run(
    session_factory: sessionmaker[Session], clock: Clock
) -> Callable[..., Callable[[], object]]:
    def make(adapters: Adapters, config: AppConfig | None = None) -> Callable[[], object]:
        ingestor = Ingestor(
            adapters,
            session_factory,
            SyncStateStore(session_factory, clock),
            config or AppConfig(),
            clock,
        )
        return ingestor.run

    return make


def rows(session_factory: sessionmaker[Session]) -> dict[str, ItemRow]:
    with session_factory() as s:
        return {r.source_id: r for r in s.scalars(select(ItemRow))}


def test_normalize_title() -> None:
    assert normalize_title("Invitation: Budget sync @ Wed Oct 16, 4pm (IST) (ceo@x.com)") == (
        "budget sync"
    )
    assert normalize_title("Updated invitation with note: Budget  Sync") == "budget sync"
    assert normalize_title("Accepted: Budget sync") == "budget sync"


def test_window_is_today_and_tomorrow_in_local_time(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    ingestor = Ingestor(
        Adapters(), session_factory, SyncStateStore(session_factory), AppConfig(), clock
    )
    start, end = ingestor.window()
    assert start == datetime(2026, 10, 15, 18, 30, tzinfo=UTC)
    assert end == start + timedelta(days=2)


def test_morning_sync_across_calendar_tasks_and_mail(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    cal = FakeCalendar(events={"m1": meeting("m1", 2), "far": meeting("far", 24 * 5)})
    tasks = FakeTasks(tasks={"OPS-1": task("OPS-1", due=NOW + timedelta(days=3))})
    mail = FakeMail(inbox=[email("e1", "Vendor contract review")])
    report = run(Adapters(calendars=[cal], tasks=[tasks], mail=[mail]))()
    assert report.created == 3  # type: ignore[attr-defined]
    items = rows(session_factory)
    assert set(items) == {"m1", "OPS-1", "mail:e1"}
    m1 = items["m1"]
    assert m1.kind == ItemKind.MEETING and m1.estimated_minutes == 30
    assert m1.participants == ["ceo@example.com", "cfo@example.com"]
    assert m1.requester == "cfo@example.com" and m1.content_hash
    assert items["OPS-1"].requester == "meera@example.com"
    assert items["mail:e1"].kind == ItemKind.EMAIL_ACTION
    assert items["mail:e1"].dedup_key == "thread:gmail:t-e1"


def test_resync_is_idempotent_and_incremental(
    run: Callable[..., Callable[[], object]],
    session_factory: sessionmaker[Session],
    clock: Clock,
) -> None:
    cal = FakeCalendar(events={"m1": meeting("m1", 2), "m2": meeting("m2", 3)})
    mail = FakeMail(inbox=[email("e1", "Hello")])
    ingest = run(Adapters(calendars=[cal], mail=[mail]))
    ingest()
    first_hash = rows(session_factory)["m1"].content_hash

    cal.changes = [meeting("m1", 2, title="Budget sync (moved)"), meeting("m2", 3, cancelled=True)]
    clock.now += timedelta(minutes=30)
    report = ingest()
    assert report.created == 0  # type: ignore[attr-defined]
    items = rows(session_factory)
    assert items["m1"].title == "Budget sync (moved)"
    assert items["m1"].content_hash != first_hash
    assert items["m2"].status == ItemStatus.DROPPED
    # The second email sync asks only for mail since the first run.
    assert mail.since_calls[1] == NOW
    assert mail.since_calls[0] == NOW - timedelta(hours=48)


def test_unchanged_content_keeps_score_changed_content_needs_rescore(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    cal = FakeCalendar(events={"m1": meeting("m1", 2)})
    ingest = run(Adapters(calendars=[cal]))
    ingest()
    with session_factory.begin() as s:
        row = s.scalars(select(ItemRow)).one()
        row.scored_at = NOW
        row.priority_score = 80
    cal.changes = [meeting("m1", 2)]
    ingest()
    assert rows(session_factory)["m1"].scored_at is not None
    cal.changes = [meeting("m1", 2, description="new agenda")]
    ingest()
    row = rows(session_factory)["m1"]
    assert row.scored_at is None and row.priority_score == 80


def test_full_resync_drops_meetings_that_vanished(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    cal = FakeCalendar(events={"m1": meeting("m1", 2), "m2": meeting("m2", 3)})
    ingest = run(Adapters(calendars=[cal]))
    ingest()
    del cal.events["m2"]
    with session_factory.begin() as s:  # force a full sync: forget the cursor
        from app.db.models import SyncStateRow

        s.query(SyncStateRow).delete()
    report = ingest()
    assert report.closed == 1  # type: ignore[attr-defined]
    assert rows(session_factory)["m2"].status == ItemStatus.DROPPED


def test_declined_by_ceo_at_source(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    declined = replace(CEO, response="declined")
    cal = FakeCalendar(events={"m1": meeting("m1", 2, attendees=(declined,))})
    run(Adapters(calendars=[cal]))()
    assert rows(session_factory)["m1"].status == ItemStatus.DECLINED


def test_same_meeting_in_two_calendars_counts_once(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    google = FakeCalendar(events={"g1": meeting("g1", 2, ical_uid="UID-1@x")})
    outlook = FakeCalendar(
        source=Source.OUTLOOK,
        events={"o1": meeting("o1", 2, source=Source.OUTLOOK, ical_uid="uid-1@x")},
    )
    report = run(Adapters(calendars=[google, outlook]))()
    assert report.duplicates == 1  # type: ignore[attr-defined]
    assert set(rows(session_factory)) == {"g1"}


def test_meeting_invite_email_counts_once(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    cal = FakeCalendar(events={"m1": meeting("m1", 2, title="Budget sync")})
    mail = FakeMail(
        inbox=[
            email("inv", "Invitation: Budget sync @ Fri Oct 16, 4pm (IST)", "cfo@example.com"),
            email("other", "Budget sync", "stranger@example.com"),
        ]
    )
    report = run(Adapters(calendars=[cal], mail=[mail]))()
    assert report.duplicates == 1  # type: ignore[attr-defined]
    assert set(rows(session_factory)) == {"m1", "mail:other"}


def test_tasks_done_or_gone_at_source_are_closed(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    tasks = FakeTasks(tasks={"A": task("A"), "B": task("B"), "C": task("C")})
    ingest = run(Adapters(tasks=[tasks]))
    ingest()
    tasks.tasks["A"] = replace(tasks.tasks["A"], done=True)
    del tasks.tasks["B"]
    report = ingest()
    items = rows(session_factory)
    assert items["A"].status == ItemStatus.DONE and items["B"].status == ItemStatus.DONE
    assert items["C"].status == ItemStatus.OPEN
    assert report.closed == 2  # type: ignore[attr-defined]


def test_ceo_decisions_survive_sync(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    tasks = FakeTasks(tasks={"A": task("A")})
    ingest = run(Adapters(tasks=[tasks]))
    ingest()
    with session_factory.begin() as s:
        s.scalars(select(ItemRow)).one().status = ItemStatus.DELEGATED
    tasks.tasks["A"] = replace(tasks.tasks["A"], title="Renamed")
    ingest()
    row = rows(session_factory)["A"]
    assert row.status == ItemStatus.DELEGATED and row.title == "Renamed"


def test_done_task_reported_by_source_is_closed_even_if_listed(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    class ListsDone(FakeTasks):
        def list_open(self) -> list[ExternalTask]:
            return list(self.tasks.values())

    tasks = ListsDone(tasks={"A": task("A")})
    ingestor = Ingestor(
        Adapters(tasks=[tasks]),
        session_factory,
        SyncStateStore(session_factory),
        AppConfig(),
        clock,
    )
    ingestor.run()
    tasks.tasks["A"] = replace(tasks.tasks["A"], done=True)
    tasks.tasks["new-done"] = task("new-done", done=True)
    ingestor.run()
    items = rows(session_factory)
    assert items["A"].status == ItemStatus.DONE
    assert "new-done" not in items


def test_one_failing_adapter_does_not_stop_the_others(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    broken = FakeCalendar(fail_with=IntegrationError("google", "HTTP 500", 500))
    tasks = FakeTasks(tasks={"A": task("A")})
    report = run(Adapters(calendars=[broken], tasks=[tasks], not_configured=["asana: x"]))()
    assert report.errors == ["gcal: google: HTTP 500"]  # type: ignore[attr-defined]
    assert report.not_configured == ["asana: x"]  # type: ignore[attr-defined]
    assert set(rows(session_factory)) == {"A"}


def test_email_can_be_disabled(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    mail = FakeMail(inbox=[email("e1", "Hi")])
    config = AppConfig.model_validate({"ingest": {"include_email": False}})
    run(Adapters(mail=[mail]), config)()
    assert rows(session_factory) == {}
    assert mail.since_calls == []


def test_incremental_changes_outside_window_are_ignored(
    run: Callable[..., Callable[[], object]], session_factory: sessionmaker[Session]
) -> None:
    cal = FakeCalendar(events={"m1": meeting("m1", 2)})
    ingest = run(Adapters(calendars=[cal]))
    ingest()
    cal.changes = [meeting("later", 24 * 7), meeting("gone", 2, cancelled=True)]
    ingest()
    assert set(rows(session_factory)) == {"m1"}
