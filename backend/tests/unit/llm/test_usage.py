from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditLogRow
from app.llm.errors import BudgetExceededError
from app.llm.usage import (
    BUDGET_EXCEEDED_ACTION,
    BudgetGuard,
    UsageLedger,
    start_of_day,
    start_of_month,
)
from tests.unit.llm.conftest import IST, MutableClock


def test_day_and_month_boundaries_use_local_time() -> None:
    # 20:00 UTC on Oct 31 is already Nov 1 in India.
    now = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    assert start_of_month(now, IST) == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)
    assert start_of_day(now, IST) == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)


def test_ledger_totals(ledger: UsageLedger, clock: MutableClock) -> None:
    ledger.record(model="claude-haiku-5-5", purpose="score", input_tokens=1000, output_tokens=100)
    ledger.record(
        model="claude-sonnet-5-5",
        purpose="converse",
        input_tokens=500,
        output_tokens=50,
        cache_read_input_tokens=2000,
    )
    totals = ledger.totals_since(datetime(2026, 10, 1, tzinfo=UTC))
    assert totals.calls == 2
    assert totals.input_tokens == 1500
    assert totals.cache_read_input_tokens == 2000
    assert totals.cost_usd > 0
    assert ledger.totals_since(clock.now.replace(hour=7)).calls == 0


def test_budget_blocks_and_notifies_once_per_month(
    ledger: UsageLedger,
    make_budget: Callable[..., BudgetGuard],
    session_factory: sessionmaker[Session],
    clock: MutableClock,
) -> None:
    notified: list[tuple[float, float]] = []
    budget = make_budget(cap=1.0, on_exceeded=lambda spend, cap: notified.append((spend, cap)))
    budget.check()
    assert not budget.is_exceeded()

    ledger.record(model="claude-sonnet-5-5", purpose="x", input_tokens=0, output_tokens=100_000)
    assert budget.is_exceeded()
    for _ in range(2):
        with pytest.raises(BudgetExceededError, match="monthly LLM budget"):
            budget.check()
    assert len(notified) == 1

    with session_factory() as s:
        entries = s.scalars(
            select(AuditLogRow).where(AuditLogRow.action == BUDGET_EXCEEDED_ACTION)
        ).all()
    assert len(entries) == 1
    assert entries[0].outcome == "rules_only"

    # A new month starts with a fresh budget.
    clock.now = datetime(2026, 11, 15, tzinfo=UTC)
    budget.check()
