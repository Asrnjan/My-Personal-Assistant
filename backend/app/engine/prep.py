"""Meeting prep: a few spoken sentences before each meeting about who is in it, what is
open between them and the CEO, and what to push for.

The scheduler sends a prep shortly before each meeting (``workday.prep_minutes_before``);
"prep me for my 3 pm" asks for one at any time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import DecisionRow, FollowUpRow, ItemRow, MemoRow
from app.domain.enums import FollowUpState, ItemKind, ItemStatus
from app.engine.briefing import clean_title, spoken_list, spoken_time
from app.engine.contacts import all_delegates
from app.integrations.registry import Adapters
from app.llm.client import LLMClient, Task
from app.llm.errors import LLMError
from app.llm.prompts import load_prompt, untrusted

log = logging.getLogger(__name__)

MAX_PEOPLE_SEARCHED = 2
EMAILS_PER_PERSON = 2

# Facts the CEO asked to remember about these people, if the memory feature is wired.
FactsLookup = Callable[[list[str]], list[str]]


class MeetingPrep:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LLMClient,
        adapters_factory: Callable[[], Adapters],
        tz: tzinfo,
        clock: Clock,
        minutes_before: int = 10,
        facts: FactsLookup | None = None,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._adapters = adapters_factory
        self._tz = tz
        self._clock = clock
        self._minutes = minutes_before
        self._facts = facts

    def set_facts(self, facts: FactsLookup) -> None:
        self._facts = facts

    # -- context -----------------------------------------------------------------

    def _context(self, s: Session, item: ItemRow) -> tuple[list[str], str]:
        """Who is attending (spoken names) and the context block for the model."""
        people = {d.email.lower(): d for d in all_delegates(s)}
        attendees = [p.lower() for p in item.participants or []]
        names = [people[a].full_name if a in people else a for a in attendees]
        lines = [
            f"Meeting: {item.title}",
            f"Starts: {spoken_time(item.start, self._tz) if item.start else 'unknown'}",
            "Attendees: " + (", ".join(names) or "none listed"),
        ]
        team_ids = {people[a].id: people[a].full_name for a in attendees if a in people}
        if team_ids:
            rows = s.execute(
                select(FollowUpRow, DecisionRow, ItemRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .join(ItemRow, DecisionRow.item_id == ItemRow.id)
                .where(
                    DecisionRow.delegate_id.in_(list(team_ids)),
                    FollowUpRow.state != FollowUpState.CLOSED,
                )
            ).all()
            for fu, dec, delegated in rows:
                due = f", due {fu.due.astimezone(self._tz):%A}" if fu.due else ""
                late = " (late)" if fu.state == FollowUpState.OVERDUE else ""
                lines.append(
                    f"Delegated to {team_ids[dec.delegate_id or '']}: "
                    f"{clean_title(delegated.title)}{due}{late}"
                )
        if attendees:
            others = s.scalars(
                select(ItemRow).where(ItemRow.status == ItemStatus.OPEN, ItemRow.id != item.id)
            )
            for other in others:
                shared = set(other.participants or []) & set(attendees)
                if shared and other.kind != ItemKind.MEETING:
                    lines.append(f"Open {other.kind.value.replace('_', ' ')}: {other.title}")
        emails = self._emails(attendees)
        if emails:
            lines.append(untrusted("recent emails", "\n".join(emails)))
        if self._facts is not None:
            facts = self._facts([*names, item.title])
            if facts:
                lines.append("Things the CEO asked you to remember:\n" + "\n".join(facts))
        if item.description:
            lines.append(untrusted("meeting description", item.description[:1500]))
        return names, "\n".join(lines)

    def _emails(self, attendees: list[str]) -> list[str]:
        try:
            mailboxes = self._adapters().mail
        except Exception:
            log.exception("could not build mail adapters for meeting prep")
            return []
        if not mailboxes:
            return []
        out: list[str] = []
        for address in attendees[:MAX_PEOPLE_SEARCHED]:
            try:
                found = mailboxes[0].search(address, limit=EMAILS_PER_PERSON)
            except Exception as exc:
                log.info("meeting prep email search failed: %s", exc)
                continue
            out += [f"From {m.sender}: {m.subject}. {m.snippet[:200]}" for m in found]
        return out

    # -- prep ----------------------------------------------------------------------

    def prepare(self, item_id: str) -> str:
        with self._sessions() as s:
            item = s.get(ItemRow, item_id)
            if item is None:
                raise ValueError("meeting not found")
            names, context = self._context(s, item)
            title = clean_title(item.title)
        try:
            response = self._llm.call(
                Task.BULK,
                "meeting_prep",
                system=[load_prompt("prep").text],
                messages=[{"role": "user", "content": context}],
                max_tokens=400,
            )
            text = " ".join(b.text for b in response.content if b.type == "text").strip()
        except LLMError as exc:
            log.info("meeting prep fell back to a template: %s", exc)
            text = ""
        if not text:
            who = spoken_list(names[:4]) if names else "no one else listed"
            text = f"{title} is with {who}."
        return text

    def due(self) -> list[tuple[str, str]]:
        """Preps for meetings starting within ``minutes_before``, each once.
        Returns (item id, text) pairs; the caller delivers them."""
        if not self._minutes:
            return []
        now = self._clock()
        with self._sessions.begin() as s:
            rows = list(
                s.scalars(
                    select(ItemRow).where(
                        ItemRow.kind == ItemKind.MEETING,
                        ItemRow.status == ItemStatus.OPEN,
                        ItemRow.start > now,
                        ItemRow.start <= now + timedelta(minutes=self._minutes),
                    )
                )
            )
            done = set(
                s.scalars(
                    select(MemoRow.key).where(
                        MemoRow.kind == "prep", MemoRow.key.in_([r.id for r in rows])
                    )
                )
            )
            todo = [(r.id, r.start) for r in rows if r.id not in done]
            # Claimed before the (slow) model call, so an overlapping run can't repeat it.
            for item_id, start in todo:
                s.add(MemoRow(kind="prep", key=item_id, text="", created_at=now, due_at=start))
        out = []
        for item_id, _start in todo:
            text = self.prepare(item_id)
            with self._sessions.begin() as s:
                memo = s.scalars(
                    select(MemoRow).where(MemoRow.kind == "prep", MemoRow.key == item_id)
                ).one()
                memo.text = text
            out.append((item_id, text))
        return out


def upcoming_meeting(s: Session, now: datetime) -> ItemRow | None:
    """The next meeting that hasn't ended, for "prep me for my next meeting"."""
    return s.scalars(
        select(ItemRow)
        .where(
            ItemRow.kind == ItemKind.MEETING,
            ItemRow.status == ItemStatus.OPEN,
            ItemRow.start.is_not(None),
            ItemRow.end > now,
        )
        .order_by(ItemRow.start)
        .limit(1)
    ).first()
