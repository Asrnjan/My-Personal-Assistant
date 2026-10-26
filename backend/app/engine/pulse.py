"""The team pulse: what each person finished, what is open and what slipped, and what
the CEO is waiting on. Built from the delegations and their follow-ups; no model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import DecisionRow, FollowUpRow, ItemRow, MemoRow
from app.domain.enums import FollowUpState
from app.engine.briefing import clean_title, plural, spoken_list
from app.engine.contacts import all_delegates, display_names
from app.engine.delegation import spoken_day

# Someone with this many open delegations is called out as stretched.
LOADED_AT = 4


@dataclass
class PersonWeek:
    name: str
    finished: list[str] = field(default_factory=list)
    open: list[str] = field(default_factory=list)
    late: list[str] = field(default_factory=list)


class TeamPulse:
    def __init__(self, session_factory: sessionmaker[Session], tz: tzinfo, clock: Clock) -> None:
        self._sessions = session_factory
        self._tz = tz
        self._clock = clock

    def _rows(self, s: Session) -> list[tuple[FollowUpRow, DecisionRow, ItemRow]]:
        return [
            (fu, dec, item)
            for fu, dec, item in s.execute(
                select(FollowUpRow, DecisionRow, ItemRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .join(ItemRow, DecisionRow.item_id == ItemRow.id)
                .order_by(FollowUpRow.due.nulls_last(), FollowUpRow.created_at)
            ).all()
        ]

    def people(self, since: datetime) -> list[PersonWeek]:
        now = self._clock()
        with self._sessions() as s:
            names = display_names(all_delegates(s))
            weeks: dict[str, PersonWeek] = {}
            for fu, dec, item in self._rows(s):
                if not dec.delegate_id:
                    continue
                week = weeks.setdefault(
                    dec.delegate_id, PersonWeek(names.get(dec.delegate_id, dec.delegate_id))
                )
                title = clean_title(item.title)
                if fu.state == FollowUpState.CLOSED:
                    if fu.updated_at >= since and dec.executed_at is not None:
                        week.finished.append(title)
                elif fu.state == FollowUpState.OVERDUE or (fu.due is not None and fu.due < now):
                    week.late.append(title)
                else:
                    week.open.append(title)
        return sorted(weeks.values(), key=lambda w: (-len(w.late), -len(w.open), w.name))

    def weekly(self) -> str:
        now = self._clock()
        people = [p for p in self.people(now - timedelta(days=7)) if p.finished or p.open or p.late]
        if not people:
            return "Team pulse: nothing was delegated this week."
        lines = ["Team pulse for the week."]
        for p in people:
            parts = []
            if p.finished:
                parts.append(f"finished {plural(len(p.finished), 'item')}")
            if p.open:
                parts.append(f"has {len(p.open)} open")
            if p.late:
                more = len(p.late) - 2
                late = spoken_list([*p.late[:2], *([f"{more} more"] if more > 0 else [])])
                parts.append(f"is late on {late}")
            lines.append(f"{p.name} {spoken_list(parts)}.")
        stretched = [p.name for p in people if len(p.open) + len(p.late) >= LOADED_AT]
        if stretched:
            lines.append(f"{spoken_list(stretched)} may be overloaded.")
        text = " ".join(lines)
        with self._sessions.begin() as s:
            key = now.astimezone(self._tz).strftime("pulse-%G-W%V")
            if (
                s.scalars(
                    select(MemoRow).where(MemoRow.kind == "report", MemoRow.key == key)
                ).first()
                is None
            ):
                s.add(MemoRow(kind="report", key=key, text=text, created_at=now))
        return text

    def waiting_on(self) -> list[str]:
        """Everything the CEO is waiting on, late work first."""
        now = self._clock()
        today = now.astimezone(self._tz).date()
        late: list[str] = []
        due: list[str] = []
        with self._sessions() as s:
            names = display_names(all_delegates(s))
            for fu, dec, item in self._rows(s):
                if fu.state == FollowUpState.CLOSED:
                    continue
                who = names.get(dec.delegate_id or "", "someone")
                title = clean_title(item.title)
                if fu.due is None:
                    due.append(f"{title} from {who}, no deadline")
                    continue
                when = spoken_day(fu.due.astimezone(self._tz).date(), today)
                if fu.due < now:
                    days = (today - fu.due.astimezone(self._tz).date()).days
                    late.append(
                        f"{title} from {who}, {plural(days, 'day')} late"
                        if days
                        else f"{title} from {who}, due earlier today"
                    )
                else:
                    unconfirmed = (
                        "" if fu.state == FollowUpState.ACKNOWLEDGED else ", not confirmed"
                    )
                    due.append(f"{title} from {who}, due {when}{unconfirmed}")
        return [*late, *due]
