"""Spoken briefs: morning brief, evening wrap-up and P1 alerts.

Briefs are composed from templates, not by the LLM: the items were already
scored, so wording them costs nothing and stays predictable. Each brief is
trimmed to fit ``briefing.max_spoken_seconds``; ``details`` holds one
sentence per item for "tell me more".
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.config import AppConfig
from app.db.models import DecisionRow, FollowUpRow, ItemRow
from app.domain.enums import FollowUpState, ItemKind, ItemStatus, PriorityBand
from app.engine.contacts import all_delegates, display_names

MAX_TITLE_WORDS = 8

Suggestion = Literal["do", "delegate", "decline", "drop", "rollover", "open"]


class BriefKind(StrEnum):
    MORNING = "morning"
    EVENING = "evening"
    ALERT = "alert"


class BriefItem(BaseModel):
    id: str
    title: str
    kind: ItemKind
    band: PriorityBand | None
    score: int | None
    reason: str | None
    start: datetime | None
    due: datetime | None
    url: str | None
    suggested: Suggestion
    delegate_id: str | None = None
    delegate_name: str | None = None


class DelegationStatus(BaseModel):
    followup_id: str
    item_id: str
    title: str
    delegate_id: str | None
    delegate_name: str | None
    state: FollowUpState
    due: datetime | None


class Brief(BaseModel):
    kind: BriefKind
    for_date: date
    text: str
    details: list[str]
    items: list[BriefItem]
    delegations: list[DelegationStatus] = []
    word_count: int
    estimated_seconds: int
    generated_at: datetime


# --- spoken-language helpers -------------------------------------------------


def clean_title(title: str) -> str:
    text = re.sub(r"[*_`#>\[\]|]", "", title)
    words = re.sub(r"\s+", " ", text).strip().rstrip(".!?").split()
    return " ".join(words[:MAX_TITLE_WORDS]) or "an untitled item"


def spoken_time(dt: datetime, tz: tzinfo) -> str:
    local = dt.astimezone(tz)
    hour = local.hour % 12 or 12
    suffix = "am" if local.hour < 12 else "pm"
    if local.minute:
        return f"{hour}:{local.minute:02d} {suffix}"
    return f"{hour} {suffix}"


def spoken_list(parts: Sequence[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def word_count(text: str) -> int:
    return len(text.split())


def fingerprint(row: ItemRow) -> str:
    """What makes a P1 "changed": its title, timing or band."""
    parts = [row.title, row.start, row.due, row.priority_band]
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


class _Section:
    """One sentence that lists items, e.g. "3 need you: A, B, and 1 more."."""

    def __init__(
        self,
        labels: list[str],
        listed: Callable[[str, int], str],
        counted: Callable[[int], str],
        empty: str = "",
    ) -> None:
        self.labels = labels
        self.listed = listed
        self.counted = counted
        self.empty = empty

    def render(self, shown: int) -> str:
        n = len(self.labels)
        if n == 0:
            return self.empty
        if shown <= 0:
            return self.counted(n)
        parts = self.labels[:shown]
        if n > shown:
            parts = [*parts, f"{n - shown} more"]
        return self.listed(spoken_list(parts), n)


def _fit(head: str, sections: list[_Section], tail: str, max_words: int) -> str:
    """Show as many items per section as fit in ``max_words``.

    Starts with every item and removes one at a time from the longest list
    until the text fits or every list is reduced to a count.
    """
    shown = [len(s.labels) for s in sections]

    def build() -> str:
        sentences = [head, *(s.render(k) for s, k in zip(sections, shown, strict=True)), tail]
        return " ".join(x for x in sentences if x)

    text = build()
    while word_count(text) > max_words and any(shown):
        i = max(range(len(shown)), key=lambda j: shown[j])
        shown[i] -= 1
        text = build()
    return text


# --- the briefer --------------------------------------------------------------


# Extra sentences for the morning brief (overlapping meetings, no focus time, late team
# work); given the session, the day and now.
MorningNotes = Callable[[Session, date, datetime], list[str]]


class Briefer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        app_config: AppConfig,
        clock: Clock,
        morning_notes: MorningNotes | None = None,
    ) -> None:
        self._sessions = session_factory
        self._config = app_config
        self._tz = app_config.tz
        self._clock = clock
        self._morning_notes = morning_notes

    def today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    def _day(self, on: date) -> tuple[datetime, datetime]:
        lo = datetime.combine(on, time.min, tzinfo=self._tz)
        return lo, lo + timedelta(days=1)

    def _label(self, row: ItemRow) -> str:
        title = clean_title(row.title)
        if row.kind == ItemKind.MEETING and row.start is not None:
            return f"{title} at {spoken_time(row.start, self._tz)}"
        return title

    def _finish(
        self,
        kind: BriefKind,
        on: date,
        text: str,
        details: list[str],
        items: list[BriefItem],
        delegations: list[DelegationStatus] | None = None,
    ) -> Brief:
        words = word_count(text)
        wpm = self._config.briefing.words_per_minute
        return Brief(
            kind=kind,
            for_date=on,
            text=text,
            details=details,
            items=items,
            delegations=delegations or [],
            word_count=words,
            estimated_seconds=max(1, round(words * 60 / wpm)),
            generated_at=self._clock(),
        )

    @staticmethod
    def _names(s: Session) -> dict[str, str]:
        return display_names(all_delegates(s))

    def _item(
        self, row: ItemRow, suggested: Suggestion, names: dict[str, str] | None = None
    ) -> BriefItem:
        delegate = row.suggested_delegate_id if suggested == "delegate" else None
        return BriefItem(
            id=row.id,
            title=row.title,
            kind=row.kind,
            band=row.priority_band,
            score=row.priority_score,
            reason=row.reason,
            start=row.start,
            due=row.due,
            url=row.url,
            suggested=suggested,
            delegate_id=delegate,
            delegate_name=(names or {}).get(delegate) if delegate else None,
        )

    @staticmethod
    def _order(rows: list[ItemRow]) -> list[ItemRow]:
        far = datetime.max.replace(tzinfo=None)
        return sorted(
            rows,
            key=lambda r: (
                -(r.priority_score or 0),
                (r.start or r.due or far).replace(tzinfo=None),
                r.title,
            ),
        )

    # -- morning ---------------------------------------------------------------

    def _agenda(self, s: Session, on: date, now: datetime) -> list[ItemRow]:
        """Open items for ``on``: its meetings (not yet over), tasks due by then
        (overdue included), undated P1 tasks, and open emails."""
        lo, hi = self._day(on)
        rows = s.scalars(
            select(ItemRow).where(
                ItemRow.status == ItemStatus.OPEN,
                or_(
                    and_(ItemRow.kind == ItemKind.MEETING, ItemRow.start >= lo, ItemRow.start < hi),
                    and_(ItemRow.kind == ItemKind.TASK, ItemRow.due < hi),
                    and_(
                        ItemRow.kind == ItemKind.TASK,
                        ItemRow.due.is_(None),
                        ItemRow.priority_band == PriorityBand.P1_DO_MYSELF,
                    ),
                    ItemRow.kind == ItemKind.EMAIL_ACTION,
                ),
            )
        )
        return [
            r
            for r in rows
            if not (r.kind == ItemKind.MEETING and r.end is not None and r.end <= now)
        ]

    def morning(self, on: date | None = None) -> Brief:
        on = on or self.today()
        now = self._clock()
        with self._sessions() as s:
            names = self._names(s)
            rows = self._order(self._agenda(s, on, now))
            notes = self._morning_notes(s, on, now) if self._morning_notes else []

        by_band: dict[PriorityBand | None, list[ItemRow]] = {}
        for r in rows:
            by_band.setdefault(r.priority_band, []).append(r)
        need = by_band.get(PriorityBand.P1_DO_MYSELF, [])
        delegate = by_band.get(PriorityBand.P2_DELEGATE, [])
        decline = by_band.get(PriorityBand.P3_DECLINE, [])
        drop = by_band.get(PriorityBand.P4_DROP, [])
        unscored = by_band.get(None, [])

        meetings = sum(r.kind == ItemKind.MEETING for r in rows)
        tasks = sum(r.kind == ItemKind.TASK for r in rows)
        emails = sum(r.kind == ItemKind.EMAIL_ACTION for r in rows)
        when = "today" if on == self.today() else f"on {on.strftime('%A')}"
        if not rows:
            head = f"Good morning. Your day is clear {when}."
        else:
            counts = [plural(meetings, "meeting"), plural(tasks, "task")]
            if emails:
                counts.append(plural(emails, "email") + " to act on")
            head = f"Good morning. You have {spoken_list(counts)} {when}."

        def delegate_label(r: ItemRow) -> str:
            name = names.get(r.suggested_delegate_id or "")
            return f"{self._label(r)} to {name}" if name else self._label(r)

        sections = [
            _Section(
                [self._label(r) for r in need],
                lambda xs, n: f"{n} {'needs' if n == 1 else 'need'} you: {xs}.",
                lambda n: f"{n} {'needs' if n == 1 else 'need'} you.",
                "Nothing needs you personally." if rows else "",
            ),
            _Section(
                [delegate_label(r) for r in delegate],
                lambda xs, n: f"I suggest delegating {xs}.",
                lambda n: f"I suggest delegating {plural(n, 'item')}.",
            ),
            _Section(
                [self._label(r) for r in decline],
                lambda xs, n: f"I suggest declining {xs}.",
                lambda n: f"I suggest declining {plural(n, 'item')}.",
            ),
            _Section(
                [self._label(r) for r in drop],
                lambda xs, n: f"You can drop {xs}.",
                lambda n: f"You can drop {plural(n, 'item')}.",
            ),
            _Section(
                [self._label(r) for r in unscored],
                lambda xs, n: f"Not yet scored: {xs}.",
                lambda n: f"{plural(n, 'item')} {'is' if n == 1 else 'are'} not scored yet.",
            ),
        ]
        # The notes go first so trimming for time never drops them.
        if notes:
            head = " ".join([head, *notes])
        tail = "Shall I go ahead?" if delegate or decline or drop else ""
        text = _fit(head, sections, tail, self._config.briefing.max_words)

        suggestion: dict[PriorityBand | None, Suggestion] = {
            PriorityBand.P1_DO_MYSELF: "do",
            PriorityBand.P2_DELEGATE: "delegate",
            PriorityBand.P3_DECLINE: "decline",
            PriorityBand.P4_DROP: "drop",
            None: "open",
        }
        ordered = [*need, *delegate, *decline, *drop, *unscored]
        items = [self._item(r, suggestion[r.priority_band], names) for r in ordered]
        details = [self._detail(i) for i in items]
        return self._finish(BriefKind.MORNING, on, text, details, items)

    def _detail(self, item: BriefItem) -> str:
        label = clean_title(item.title)
        if item.kind == ItemKind.MEETING and item.start is not None:
            label += f" at {spoken_time(item.start, self._tz)}"
        elif item.due is not None:
            when = self._due_phrase(item.due)
            label += ", overdue" if when == "overdue" else f", due {when}"
        reason = item.reason or "It has not been scored yet."
        extra = {
            "do": "",
            "delegate": f" I suggest {item.delegate_name}." if item.delegate_name else "",
            "decline": " I suggest declining.",
            "drop": " I suggest dropping it.",
            "rollover": " I suggest moving it to tomorrow.",
            "open": "",
        }[item.suggested]
        return f"{label}. {reason}{extra}"

    def _due_phrase(self, due: datetime) -> str:
        day = due.astimezone(self._tz).date()
        today = self.today()
        if day < today:
            return "overdue"
        if day == today:
            return "today"
        if day == today + timedelta(days=1):
            return "tomorrow"
        return f"on {day.strftime('%A')}"

    # -- evening ---------------------------------------------------------------

    def _delegations(
        self, s: Session, names: dict[str, str], since: datetime
    ) -> tuple[list[DelegationStatus], list[DelegationStatus]]:
        """Unacknowledged follow-ups, and those acknowledged since ``since``."""
        rows = s.execute(
            select(FollowUpRow, DecisionRow, ItemRow)
            .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
            .join(ItemRow, DecisionRow.item_id == ItemRow.id)
            .order_by(FollowUpRow.due.nulls_last(), FollowUpRow.created_at)
        ).all()
        waiting: list[DelegationStatus] = []
        accepted: list[DelegationStatus] = []
        for fu, dec, item in rows:
            status = DelegationStatus(
                followup_id=fu.id,
                item_id=item.id,
                title=item.title,
                delegate_id=dec.delegate_id,
                delegate_name=names.get(dec.delegate_id or ""),
                state=fu.state,
                due=fu.due,
            )
            if fu.state in {FollowUpState.PENDING, FollowUpState.NUDGED, FollowUpState.OVERDUE}:
                waiting.append(status)
            elif (
                fu.state == FollowUpState.ACKNOWLEDGED
                and fu.acknowledged_at is not None
                and fu.acknowledged_at >= since
            ):
                accepted.append(status)
        return waiting, accepted

    def evening(self, on: date | None = None) -> Brief:
        on = on or self.today()
        now = self._clock()
        lo, hi = self._day(on)
        with self._sessions() as s:
            names = self._names(s)
            open_rows = self._order(
                [
                    r
                    for r in s.scalars(
                        select(ItemRow).where(
                            ItemRow.status == ItemStatus.OPEN,
                            or_(
                                and_(ItemRow.kind == ItemKind.TASK, ItemRow.due < hi),
                                ItemRow.kind == ItemKind.EMAIL_ACTION,
                            ),
                        )
                    )
                    if r.priority_band != PriorityBand.P4_DROP
                ]
            )
            tonight = self._order(
                list(
                    s.scalars(
                        select(ItemRow).where(
                            ItemRow.status == ItemStatus.OPEN,
                            ItemRow.kind == ItemKind.MEETING,
                            ItemRow.start >= now,
                            ItemRow.start < hi,
                        )
                    )
                )
            )
            tomorrow_first = s.scalars(
                select(ItemRow)
                .where(
                    ItemRow.status == ItemStatus.OPEN,
                    ItemRow.kind == ItemKind.MEETING,
                    ItemRow.start >= hi,
                    ItemRow.start < hi + timedelta(days=1),
                )
                .order_by(ItemRow.start)
                .limit(1)
            ).first()
            waiting, accepted = self._delegations(s, names, lo)

        rollover = [r for r in open_rows if r.kind == ItemKind.TASK]
        head = "Good evening."
        if not open_rows and not tonight:
            head += " Everything on today's list is handled."

        def by_whom(d: DelegationStatus) -> str:
            title = clean_title(d.title)
            return f"{title} from {d.delegate_name}" if d.delegate_name else title

        sections = [
            _Section(
                [self._label(r) for r in tonight],
                lambda xs, n: f"Still ahead tonight: {xs}.",
                lambda n: f"You still have {plural(n, 'meeting')} tonight.",
            ),
            _Section(
                [self._label(r) for r in open_rows],
                lambda xs, n: f"{plural(n, 'item')} {'is' if n == 1 else 'are'} still open: {xs}.",
                lambda n: f"{plural(n, 'item')} {'is' if n == 1 else 'are'} still open.",
            ),
            _Section(
                [by_whom(d) for d in waiting],
                lambda xs, n: f"Still waiting to hear back on {xs}.",
                lambda n: (
                    f"{plural(n, 'delegation')} {'is' if n == 1 else 'are'} not acknowledged yet."
                ),
            ),
            _Section(
                [
                    f"{d.delegate_name or 'Someone'} accepted {clean_title(d.title)}"
                    for d in accepted
                ],
                lambda xs, n: f"{xs}.",
                lambda n: (
                    f"{plural(n, 'delegation')} {'was' if n == 1 else 'were'} accepted today."
                ),
            ),
        ]
        tail_parts: list[str] = []
        if rollover:
            if len(rollover) == len(open_rows):
                tail_parts.append(
                    "Shall I move it to tomorrow?"
                    if len(rollover) == 1
                    else "Shall I move them to tomorrow?"
                )
            else:
                moved = (
                    spoken_list([self._label(r) for r in rollover])
                    if len(rollover) <= 3
                    else f"the {plural(len(rollover), 'task')}"
                )
                tail_parts.append(f"Shall I move {moved} to tomorrow?")
        if tomorrow_first is not None:
            tail_parts.append(f"Tomorrow starts with {self._label(tomorrow_first)}.")
        text = _fit(head, sections, " ".join(tail_parts), self._config.briefing.max_words)

        items = [
            *(self._item(r, "open") for r in tonight),
            *(self._item(r, "rollover" if r.kind == ItemKind.TASK else "open") for r in open_rows),
        ]
        details = [self._detail(i) for i in items]
        details += [
            f"{clean_title(d.title)}: waiting on {d.delegate_name or 'the delegate'}"
            + (f", due {self._due_phrase(d.due)}." if d.due else ".")
            for d in waiting
        ]
        return self._finish(BriefKind.EVENING, on, text, details, items, [*waiting, *accepted])

    # -- alerts ----------------------------------------------------------------

    def alerts(self) -> Brief | None:
        """A brief about P1 items that are new or changed since the CEO last
        heard about them, or ``None``. Marks them as alerted."""
        now = self._clock()
        with self._sessions.begin() as s:
            rows = [
                r
                for r in s.scalars(
                    select(ItemRow).where(
                        ItemRow.status == ItemStatus.OPEN,
                        ItemRow.priority_band == PriorityBand.P1_DO_MYSELF,
                    )
                )
                if r.alerted_hash != fingerprint(r)
                and not (r.kind == ItemKind.MEETING and (r.end or r.start or now) < now)
            ]
            if not rows:
                return None
            rows = self._order(rows)
            changed = [r for r in rows if r.alerted_hash is not None]
            new = [r for r in rows if r.alerted_hash is None]
            for r in rows:
                r.alerted_hash = fingerprint(r)
            items = [self._item(r, "do") for r in rows]

        if len(rows) == 1:
            row = rows[0]
            what = "Update on a top priority" if changed else "New top priority"
            text = f"Heads up. {what}: {self._label(row)}. {row.reason or ''}".strip()
        else:
            sections = [
                _Section(
                    [self._label(r) for r in new],
                    lambda xs, n: (
                        f"{n} new top priorities: {xs}." if n > 1 else f"A new top priority: {xs}."
                    ),
                    lambda n: f"{plural(n, 'new top priority', 'new top priorities')}.",
                ),
                _Section(
                    [self._label(r) for r in changed],
                    lambda xs, n: f"Changed: {xs}.",
                    lambda n: f"{plural(n, 'top priority', 'top priorities')} changed.",
                ),
            ]
            text = _fit("Heads up.", sections, "", self._config.briefing.max_words)
        details = [self._detail(i) for i in items]
        return self._finish(BriefKind.ALERT, self.today(), text, details, items)

    def mark_alerted(self, item_ids: Sequence[str]) -> None:
        """Record that the CEO has heard about these P1 items (e.g. in a brief)."""
        if not item_ids:
            return
        with self._sessions.begin() as s:
            for row in s.scalars(select(ItemRow).where(ItemRow.id.in_(item_ids))):
                if row.priority_band == PriorityBand.P1_DO_MYSELF:
                    row.alerted_hash = fingerprint(row)


__all__ = [
    "Brief",
    "BriefItem",
    "BriefKind",
    "Briefer",
    "DelegationStatus",
    "clean_title",
    "fingerprint",
    "spoken_list",
    "spoken_time",
]
