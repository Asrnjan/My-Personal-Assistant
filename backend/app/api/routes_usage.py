"""LLM token usage and budget status."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.auth import AuthenticatedClient
from app.llm.usage import BudgetGuard, UsageLedger, UsageTotals, start_of_day, start_of_month

router = APIRouter(tags=["usage"])


class Totals(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: float

    @classmethod
    def of(cls, t: UsageTotals) -> Totals:
        return cls(
            calls=t.calls,
            input_tokens=t.input_tokens,
            output_tokens=t.output_tokens,
            cache_creation_input_tokens=t.cache_creation_input_tokens,
            cache_read_input_tokens=t.cache_read_input_tokens,
            cost_usd=round(t.cost_usd, 6),
        )


class UsageReport(BaseModel):
    today: Totals
    month: Totals
    monthly_budget_usd: float
    budget_exceeded: bool


@router.get("/usage", response_model=UsageReport)
def usage(request: Request, _client: AuthenticatedClient) -> UsageReport:
    ledger: UsageLedger = request.app.state.usage_ledger
    budget: BudgetGuard = request.app.state.budget
    tz = request.app.state.app_config.tz
    now = request.app.state.clock()
    month = ledger.totals_since(start_of_month(now, tz))
    return UsageReport(
        today=Totals.of(ledger.totals_since(start_of_day(now, tz))),
        month=Totals.of(month),
        monthly_budget_usd=budget.monthly_cap_usd,
        budget_exceeded=month.cost_usd >= budget.monthly_cap_usd,
    )
