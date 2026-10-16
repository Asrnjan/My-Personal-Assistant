"""Token-usage ledger and monthly budget enforcement."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditLogRow, LLMUsageRow
from app.llm.errors import BudgetExceededError
from app.llm.pricing import estimate_cost

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
BUDGET_EXCEEDED_ACTION = "llm.budget_exceeded"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class UsageTotals:
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: float


class UsageLedger:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock = utc_now) -> None:
        self._sessions = session_factory
        self._clock = clock

    def record(
        self,
        *,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        request_id: str | None = None,
    ) -> float:
        cost = estimate_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
        with self._sessions.begin() as s:
            s.add(
                LLMUsageRow(
                    at=self._clock(),
                    model=model,
                    purpose=purpose,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_input_tokens=cache_creation_input_tokens,
                    cache_read_input_tokens=cache_read_input_tokens,
                    cost_usd=cost,
                    request_id=request_id,
                )
            )
        log.info(
            "llm call purpose=%s model=%s in=%d out=%d cache_write=%d cache_read=%d cost=$%.6f",
            purpose,
            model,
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
            cost,
        )
        return cost

    def totals_since(self, since: datetime) -> UsageTotals:
        stmt = select(
            func.count(LLMUsageRow.id),
            func.coalesce(func.sum(LLMUsageRow.input_tokens), 0),
            func.coalesce(func.sum(LLMUsageRow.output_tokens), 0),
            func.coalesce(func.sum(LLMUsageRow.cache_creation_input_tokens), 0),
            func.coalesce(func.sum(LLMUsageRow.cache_read_input_tokens), 0),
            func.coalesce(func.sum(LLMUsageRow.cost_usd), 0.0),
        ).where(LLMUsageRow.at >= since)
        with self._sessions() as s:
            row = s.execute(stmt).one()
        return UsageTotals(
            calls=int(row[0]),
            input_tokens=int(row[1]),
            output_tokens=int(row[2]),
            cache_creation_input_tokens=int(row[3]),
            cache_read_input_tokens=int(row[4]),
            cost_usd=float(row[5]),
        )


def start_of_day(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def start_of_month(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


class BudgetGuard:
    """Blocks Claude calls once the month's estimated spend reaches the cap.

    The first time the cap is hit in a month an audit entry is written and
    ``on_exceeded`` is called, so the CEO can be told once rather than on
    every blocked call.
    """

    def __init__(
        self,
        ledger: UsageLedger,
        session_factory: sessionmaker[Session],
        *,
        monthly_cap_usd: float,
        tz: ZoneInfo,
        clock: Clock = utc_now,
        on_exceeded: Callable[[float, float], None] | None = None,
    ) -> None:
        self._ledger = ledger
        self._sessions = session_factory
        self.monthly_cap_usd = monthly_cap_usd
        self._tz = tz
        self._clock = clock
        self._on_exceeded = on_exceeded

    def month_spend(self) -> float:
        return self._ledger.totals_since(start_of_month(self._clock(), self._tz)).cost_usd

    def is_exceeded(self) -> bool:
        return self.month_spend() >= self.monthly_cap_usd

    def check(self) -> None:
        spend = self.month_spend()
        if spend < self.monthly_cap_usd:
            return
        self._notify_once(spend)
        raise BudgetExceededError(
            f"monthly LLM budget of ${self.monthly_cap_usd:.2f} reached (spent ${spend:.2f})"
        )

    def _notify_once(self, spend: float) -> None:
        month_start = start_of_month(self._clock(), self._tz)
        with self._sessions.begin() as s:
            already = s.scalar(
                select(func.count(AuditLogRow.id)).where(
                    AuditLogRow.action == BUDGET_EXCEEDED_ACTION, AuditLogRow.at >= month_start
                )
            )
            if already:
                return
            s.add(
                AuditLogRow(
                    at=self._clock(),
                    actor="auto",
                    action=BUDGET_EXCEEDED_ACTION,
                    outcome="rules_only",
                    details={"spend_usd": round(spend, 6), "cap_usd": self.monthly_cap_usd},
                )
            )
        log.warning(
            "LLM budget exceeded: $%.2f of $%.2f; rules-only mode", spend, self.monthly_cap_usd
        )
        if self._on_exceeded is not None:
            self._on_exceeded(spend, self.monthly_cap_usd)
