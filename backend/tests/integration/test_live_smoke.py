"""Calls the real Claude API. Opt in with RUN_LIVE_TESTS=1 and ANTHROPIC_API_KEY set."""

from __future__ import annotations

import os

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import ModelsConfig
from app.llm.client import AnthropicSender, LLMClient, Task
from app.llm.schemas import AckClassification
from app.llm.usage import BudgetGuard, UsageLedger
from tests.unit.llm.conftest import IST

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to call the real API"
)


def test_live_ack_classification(session_factory: sessionmaker[Session]) -> None:
    models = ModelsConfig()
    ledger = UsageLedger(session_factory)
    budget = BudgetGuard(ledger, session_factory, monthly_cap_usd=1.0, tz=IST)
    llm = LLMClient(AnthropicSender(None, models.max_retries), models, ledger, budget)
    result = llm.structured(
        Task.BULK,
        "live_smoke",
        system=["Classify whether the delegate's reply accepts the task."],
        user="Reply from Rahul: Accepted, on it.",
        schema=AckClassification,
        max_tokens=1024,
    )
    assert result.acknowledged is True
