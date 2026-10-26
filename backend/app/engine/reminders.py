"""Reminders: things the CEO asked to be reminded of, promises they made in their own
emails ("I'll send the deck by Friday"), and people who keep writing without an answer.

Promises are found by reading sent mail once in a while (one cheap model call); explicit
reminders are delivered at their time by the scheduler; both show up in the morning brief
on the day they are due.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import ItemRow, MemoRow
from app.domain.enums import ItemKind, ItemStatus
from app.integrations.registry import Adapters
from app.llm.client import LLMClient, Task
from app.llm.errors import LLMError
from app.llm.prompts import load_prompt, untrusted
from app.llm.schemas import Promises

log = logging.getLogger(__name__)

SENT_LOOKBACK_DAYS = 3
MAX_SENT = 30
SCAN_FEED = "sent_scan"


class Reminders:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LLMClient,
        adapters_factory: Callable[[], Adapters],
        tz: tzinfo,
        clock: Clock,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._adapters = adapters_factory
        self._tz = tz
        self._clock = clock

    # -- explicit reminders -------------------------------------------------------

    def add(self, text: str, at: datetime) -> MemoRow:
        with self._sessions.begin() as s:
            memo = MemoRow(
                kind="reminder",
                key=f"{at.isoformat()}|{text[:180]}",
                text=text,
                data={},
                created_at=self._clock(),
                due_at=at,
            )
            s.add(memo)
            s.flush()
            s.expunge(memo)
            return memo

    def due_now(self) -> list[str]:
        """Explicit reminders whose time has come, each delivered once."""
        now = self._clock()
        with self._sessions.begin() as s:
            rows = list(
                s.scalars(
                    select(MemoRow).where(
                        MemoRow.kind == "reminder",
                        MemoRow.active.is_(True),
                        MemoRow.due_at <= now,
                    )
                )
            )
            for r in rows:
                r.active = False
            return [r.text for r in rows]

    # -- promises from sent mail -------------------------------------------------------

    def scan_sent(self) -> int:
        """Find new promises in mail the CEO sent. Returns how many were stored."""
        try:
            mailboxes = self._adapters().mail
        except Exception:
            log.exception("could not build mail adapters to read sent mail")
            return 0
        if not mailboxes:
            return 0
        now = self._clock()
        with self._sessions() as s:
            seen = set(s.scalars(select(MemoRow.key).where(MemoRow.kind == SCAN_FEED)))
        messages = []
        for box in mailboxes:
            try:
                messages += box.list_sent(now - timedelta(days=SENT_LOOKBACK_DAYS), MAX_SENT)
            except Exception as exc:
                log.info("could not read sent mail: %s", exc)
        fresh = [m for m in messages if m.id not in seen][:MAX_SENT]
        if not fresh:
            return 0
        text = "\n\n".join(
            f"message_id: {m.id}\nSent: {m.received_at:%A %d %B %Y}\nTo: "
            f"{', '.join(m.recipients)}\nSubject: {m.subject}\n{m.snippet[:600]}"
            if m.received_at
            else f"message_id: {m.id}\nTo: {', '.join(m.recipients)}\nSubject: {m.subject}\n"
            f"{m.snippet[:600]}"
            for m in fresh
        )
        try:
            found = self._llm.structured(
                Task.BULK,
                "promises",
                system=[load_prompt("promises").text],
                user=untrusted("sent emails", text),
                schema=Promises,
                max_tokens=1500,
            )
        except LLMError as exc:
            log.info("could not read promises from sent mail: %s", exc)
            return 0
        valid = {m.id for m in fresh}
        stored = 0
        with self._sessions.begin() as s:
            for m in fresh:
                s.add(MemoRow(kind=SCAN_FEED, key=m.id, text="", created_at=now))
            for n, p in enumerate(found.promises):
                if p.message_id not in valid or not p.what.strip():
                    continue
                due = _due(p.due, self._tz) or now + timedelta(days=2)
                s.add(
                    MemoRow(
                        kind="promise",
                        key=f"{p.message_id}:{n}",
                        text=f"You told {p.to} you'd {p.what.strip().rstrip('.')}",
                        data={"to": p.to, "what": p.what},
                        created_at=now,
                        due_at=due,
                    )
                )
                stored += 1
        return stored

    def due_on(self, s: Session, on: date) -> list[str]:
        """Promises and reminders due by the end of ``on`` that are still open."""
        end = datetime.combine(on, time.max, tzinfo=self._tz)
        rows = s.scalars(
            select(MemoRow)
            .where(
                MemoRow.kind.in_(["promise", "reminder"]),
                MemoRow.active.is_(True),
                MemoRow.due_at <= end,
            )
            .order_by(MemoRow.due_at)
        )
        out = []
        for r in rows:
            when = "today" if r.due_at and r.due_at.astimezone(self._tz).date() == on else ""
            out.append(f"{r.text} {when}".strip() + ".")
        return out

    def done(self, text: str) -> int:
        """Mark promises and reminders matching ``text`` as done."""
        words = {w for w in text.lower().split() if len(w) > 3}
        with self._sessions.begin() as s:
            rows = list(
                s.scalars(
                    select(MemoRow).where(
                        MemoRow.kind.in_(["promise", "reminder"]), MemoRow.active.is_(True)
                    )
                )
            )
            closed = 0
            for r in rows:
                if words and len(words & set(r.text.lower().split())) >= max(1, len(words) // 2):
                    r.active = False
                    closed += 1
            return closed


def unanswered_senders(s: Session, minimum: int = 2) -> list[tuple[str, int]]:
    """People with several open emails waiting on the CEO, most first."""
    rows = s.scalars(
        select(ItemRow.requester).where(
            ItemRow.kind == ItemKind.EMAIL_ACTION,
            ItemRow.status == ItemStatus.OPEN,
            ItemRow.requester.is_not(None),
        )
    )
    counts = Counter(r for r in rows if r)
    return [(who, n) for who, n in counts.most_common() if n >= minimum]


def _due(value: str | None, tz: tzinfo) -> datetime | None:
    try:
        day = date.fromisoformat(value) if value else None
    except ValueError:
        return None
    return datetime.combine(day, time(9, 0), tzinfo=tz) if day else None
