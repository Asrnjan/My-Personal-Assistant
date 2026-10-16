from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import ModelsConfig
from app.llm.client import LLMClient
from app.llm.fake import FakeSender
from app.llm.usage import BudgetGuard, UsageLedger

IST = ZoneInfo("Asia/Kolkata")


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 10, 16, 6, 0, tzinfo=UTC))


@pytest.fixture
def ledger(session_factory: sessionmaker[Session], clock: MutableClock) -> UsageLedger:
    return UsageLedger(session_factory, clock)


@pytest.fixture
def make_budget(
    ledger: UsageLedger, session_factory: sessionmaker[Session], clock: MutableClock
) -> Callable[..., BudgetGuard]:
    def make(cap: float = 20.0, **kwargs: object) -> BudgetGuard:
        return BudgetGuard(
            ledger,
            session_factory,
            monthly_cap_usd=cap,
            tz=IST,
            clock=clock,
            **kwargs,  # type: ignore[arg-type]
        )

    return make


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def make_llm(
    sender: FakeSender, ledger: UsageLedger, make_budget: Callable[..., BudgetGuard]
) -> Callable[..., LLMClient]:
    def make(cap: float = 20.0, **models: object) -> LLMClient:
        return LLMClient(sender, ModelsConfig.model_validate(models), ledger, make_budget(cap))

    return make
