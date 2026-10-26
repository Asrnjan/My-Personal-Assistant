"""What a good chief of staff notices about a day before it starts: meetings that overlap,
no time left to think, and work the team owes that is late. Plain rules, no model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import WorkdayConfig
from app.db.models import DecisionRow, FollowUpRow, ItemRow
from app.domain.enums import FollowUpState, ItemKind, ItemStatus
from app.engine.briefing import clean_title, spoken_time
from app.engine.contacts import all_delegates, display_names


@dataclass
class DayInsights:
    # Pairs of meetings that overlap, as spoken labels.
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    # The longest free stretch inside working hours, in minutes (None: day already over).
    longest_free_minutes: int | None = None
    focus_minutes: int = 90
    # Late work the team owes the CEO: "Vendor review from Rahul".
    team_overdue: list[str] = field(default_factory=list)

    @property
    def no_focus_time(self) -> bool:
        return self.longest_free_minutes is not None and (
            self.longest_free_minutes < self.focus_minutes
        )

    def sentences(self) -> list[str]:
        out: list[str] = []
        for a, b in self.conflicts[:2]:
            out.append(f"Heads up: {a} overlaps with {b}.")
        if len(self.conflicts) > 2:
            out.append(f"{len(self.conflicts) - 2} more meetings overlap.")
        if self.no_focus_time:
            out.append(
                "You have no free block for deep work today. "
                "Say block focus time and I'll hold some."
            )
        if self.team_overdue:
            shown = self.team_overdue[:3]
            more = len(self.team_overdue) - len(shown)
            listed = ", ".join(shown) + (f", and {more} more" if more else "")
            n = len(self.team_overdue)
            out.append(f"{n} delegated {'item is' if n == 1 else 'items are'} late: {listed}.")
        return out


def _meetings(s: Session, lo: datetime, hi: datetime) -> list[ItemRow]:
    rows = s.scalars(
        select(ItemRow)
        .where(
            ItemRow.kind == ItemKind.MEETING,
            ItemRow.status == ItemStatus.OPEN,
            ItemRow.start.is_not(None),
            ItemRow.start < hi,
            ItemRow.start >= lo - timedelta(hours=12),
        )
        .order_by(ItemRow.start)
    )
    return [r for r in rows if r.end is None or r.end > lo]


def _spans(meetings: list[ItemRow]) -> list[tuple[datetime, datetime, str]]:
    return [(m.start, m.end, m.title) for m in meetings if m.start and m.end]


def conflicts(meetings: list[ItemRow], tz: tzinfo) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    spans = sorted(_spans(meetings))
    for i, (a_start, a_end, a_title) in enumerate(spans):
        for b_start, _b_end, b_title in spans[i + 1 :]:
            if b_start >= a_end:
                break
            found.append(
                (
                    f"{clean_title(a_title)} at {spoken_time(a_start, tz)}",
                    f"{clean_title(b_title)} at {spoken_time(b_start, tz)}",
                )
            )
    return found


def longest_free(meetings: list[ItemRow], lo: datetime, hi: datetime, now: datetime) -> int | None:
    """Minutes of the longest gap between meetings in ``[max(lo, now), hi)``."""
    start = max(lo, now)
    if start >= hi:
        return None
    busy = sorted(
        (max(m_start, start), min(m_end, hi))
        for m_start, m_end, _ in _spans(meetings)
        if m_end > start and m_start < hi
    )
    best = timedelta(0)
    cursor = start
    for b_start, b_end in busy:
        if b_start > cursor:
            best = max(best, b_start - cursor)
        cursor = max(cursor, b_end)
    best = max(best, hi - cursor)
    return int(best.total_seconds() // 60)


def team_overdue(s: Session, now: datetime) -> list[str]:
    names = display_names(all_delegates(s))
    rows = s.execute(
        select(FollowUpRow, DecisionRow, ItemRow)
        .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
        .join(ItemRow, DecisionRow.item_id == ItemRow.id)
        .where(FollowUpRow.state != FollowUpState.CLOSED, FollowUpRow.due.is_not(None))
        .order_by(FollowUpRow.due)
    ).all()
    out = []
    for fu, dec, item in rows:
        if fu.state == FollowUpState.OVERDUE or (fu.due is not None and fu.due < now):
            who = names.get(dec.delegate_id or "")
            out.append(f"{clean_title(item.title)} from {who}" if who else clean_title(item.title))
    return out


def day_insights(
    s: Session, on: date, now: datetime, tz: tzinfo, workday: WorkdayConfig
) -> DayInsights:
    lo = datetime.combine(on, workday.start, tzinfo=tz)
    hi = datetime.combine(on, workday.end, tzinfo=tz)
    day_lo = datetime.combine(on, time.min, tzinfo=tz)
    meetings = _meetings(s, day_lo, day_lo + timedelta(days=1))
    return DayInsights(
        conflicts=conflicts(meetings, tz),
        longest_free_minutes=longest_free(meetings, lo, hi, now),
        focus_minutes=workday.focus_minutes,
        team_overdue=team_overdue(s, now),
    )
