from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    AuditLogRow,
    Base,
    DecisionRow,
    DelegateRow,
    FollowUpRow,
    ImmutableRecordError,
    ItemRow,
    LearningSignalRow,
)
from app.db.session import make_engine, session_scope
from app.domain.enums import (
    Actor,
    ApprovedVia,
    DecisionAction,
    ItemKind,
    PriorityBand,
    SignalKind,
    Source,
)
from app.domain.item import Item


def test_migrations_match_models(database_url: str) -> None:
    engine = make_engine(database_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_round_trip_item_decision_followup(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as s:
        s.add(DelegateRow(id="rahul", full_name="Rahul Kumar", email="rahul@example.com"))
        item = ItemRow(
            source=Source.JIRA,
            source_id="OPS-12",
            kind=ItemKind.TASK,
            title="Vendor contract review",
            participants=["ceo@example.com"],
            due=datetime(2026, 10, 19, tzinfo=UTC),
            priority_band=PriorityBand.P2_DELEGATE,
            suggested_delegate_id="rahul",
        )
        s.add(item)
        s.flush()
        dec = DecisionRow(
            item_id=item.id,
            action=DecisionAction.DELEGATE,
            actor=Actor.CEO,
            approved_via=ApprovedVia.VOICE,
            delegate_id="rahul",
            channels_used=["email"],
        )
        s.add(dec)
        s.flush()
        s.add(FollowUpRow(decision_id=dec.id))
        s.add(LearningSignalRow(kind=SignalKind.RULE, rule_text="anything from board = P1"))
        item_id = item.id

    with session_factory() as s:
        row = s.get(ItemRow, item_id)
        assert row is not None
        domain = Item.model_validate(row)
        assert domain.priority_band == PriorityBand.P2_DELEGATE
        assert domain.participants == ["ceo@example.com"]
        assert row.decisions[0].followups[0].nudges_sent == 0


def test_source_id_unique_per_source(session_factory: sessionmaker[Session]) -> None:
    def add(s: Session) -> None:
        s.add(ItemRow(source=Source.GCAL, source_id="e1", kind=ItemKind.MEETING, title="Sync"))

    with session_factory.begin() as s:
        add(s)
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        add(s)


def test_foreign_keys_enforced(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(IntegrityError), session_factory.begin() as s:
        s.add(
            ItemRow(
                source=Source.GCAL,
                source_id="e2",
                kind=ItemKind.MEETING,
                title="Sync",
                suggested_delegate_id="nobody",
            )
        )


def test_audit_log_is_immutable(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as s:
        s.add(AuditLogRow(actor="ceo", action="email.send", outcome="ok", details={"to": "r"}))
    with session_factory() as s:
        entry = s.scalars(select(AuditLogRow)).one()
        entry.outcome = "tampered"
        with pytest.raises(ImmutableRecordError):
            s.flush()
        s.rollback()
        s.delete(entry)
        with pytest.raises(ImmutableRecordError):
            s.flush()


def test_session_scope_commits_and_rolls_back(session_factory: sessionmaker[Session]) -> None:
    gen = session_scope(session_factory)
    s = next(gen)
    s.add(DelegateRow(id="meera", full_name="Meera", email="m@example.com"))
    with pytest.raises(StopIteration):
        next(gen)

    gen = session_scope(session_factory)
    s = next(gen)
    s.add(DelegateRow(id="ghost", full_name="Ghost", email="g@example.com"))
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("boom"))

    with session_factory() as s:
        ids = set(s.scalars(select(DelegateRow.id)))
    assert ids == {"meera"}


def test_datetimes_round_trip_as_utc_and_naive_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    from zoneinfo import ZoneInfo

    local = datetime(2026, 10, 16, 14, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    with session_factory.begin() as s:
        s.add(
            ItemRow(
                source=Source.GCAL, source_id="tz", kind=ItemKind.MEETING, title="T", start=local
            )
        )
    with session_factory() as s:
        row = s.scalars(select(ItemRow).where(ItemRow.source_id == "tz")).one()
    assert row.start == local
    assert row.start is not None and row.start.tzinfo == UTC

    with pytest.raises(Exception, match="naive"), session_factory.begin() as s:
        s.add(
            ItemRow(
                source=Source.GCAL,
                source_id="n",
                kind=ItemKind.MEETING,
                title="T",
                start=datetime(2026, 1, 1),
            )
        )
