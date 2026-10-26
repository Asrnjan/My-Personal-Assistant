"""What the assistant did for the CEO, and undoing it.

The weekly report counts everything the CEO approved (or let run on its own) in the last
seven days. Undo reverses what can be reversed: a deferred, dropped or closed item comes
back, a team change is reverted, and a moved meeting goes back to its old time after a yes.
Sent emails and messages can't be unsent; the CEO is told so.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import audit
from app.clock import Clock
from app.db.models import DelegateRow, ItemRow, MemoRow, PendingActionRow
from app.domain.delegate import Delegate
from app.domain.enums import ActionKind, ActionStatus, ItemStatus
from app.engine.briefing import plural, spoken_list
from app.engine.memory import keywords

_DONE_LABELS: dict[ActionKind, tuple[str, str]] = {
    ActionKind.DELEGATE: ("delegated", "item"),
    ActionKind.SEND_EMAIL: ("sent", "email"),
    ActionKind.REPLY_EMAIL: ("answered", "email"),
    ActionKind.TEAMS_MESSAGE: ("sent", "Teams message"),
    ActionKind.BOOK_MEETING: ("booked", "meeting"),
    ActionKind.MOVE_MEETING: ("moved", "meeting"),
    ActionKind.DECLINE_MEETING: ("declined", "meeting"),
    ActionKind.DEFER: ("deferred", "item"),
    ActionKind.CLOSE_TASK: ("closed", "task"),
    ActionKind.DROP: ("dropped", "item"),
    ActionKind.ADD_CONTACT: ("added", "person to your team"),
    ActionKind.REMOVE_CONTACT: ("removed", "person from your team"),
}
_CANNOT_UNDO = {
    ActionKind.SEND_EMAIL: "An email can't be unsent. I can send a follow-up correcting it.",
    ActionKind.REPLY_EMAIL: "A reply can't be unsent. I can send a follow-up correcting it.",
    ActionKind.TEAMS_MESSAGE: "A Teams message can't be unsent from here.",
    ActionKind.BOOK_MEETING: "I can't cancel meetings; please delete it in your calendar.",
    ActionKind.DECLINE_MEETING: "The organizer already has your decline; reply to them to rejoin.",
    ActionKind.DELEGATE: "The delegate already has it. I can send them a note taking it back.",
}


UNDONE = "(Undone.)"


@dataclass
class Undo:
    """What happened: ``done`` when reversed already, ``propose`` holds a read-back and
    the params for a meeting move that needs a yes."""

    message: str
    done: bool = False
    propose: tuple[str, str, dict[str, str]] | None = None  # (item id, read-back, params)


class Activity:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._sessions = session_factory
        self._clock = clock

    def _executed(self, s: Session, since: datetime) -> list[PendingActionRow]:
        return list(
            s.scalars(
                select(PendingActionRow)
                .where(
                    PendingActionRow.status == ActionStatus.EXECUTED,
                    PendingActionRow.resolved_at >= since,
                )
                .order_by(PendingActionRow.resolved_at.desc(), PendingActionRow.created_at.desc())
            )
        )

    def lines(self, days: int = 7) -> list[str]:
        """One line per action, newest first, for "what did you do this week?"."""
        since = self._clock() - timedelta(days=days)
        with self._sessions() as s:
            return [r.result or r.readback for r in self._executed(s, since)]

    def weekly(self) -> str:
        now = self._clock()
        with self._sessions() as s:
            rows = self._executed(s, now - timedelta(days=7))
        if not rows:
            return "This week I didn't act on anything for you."
        counts = Counter(r.kind for r in rows)
        parts = []
        for kind, (verb, noun) in _DONE_LABELS.items():
            n = counts.get(kind, 0)
            if n:
                many = "people" if noun.startswith("person") else None
                label = plural(n, noun.split()[0], many)
                rest = " ".join(noun.split()[1:])
                parts.append(f"{verb} {label}{' ' + rest if rest else ''}")
        text = f"This week I {spoken_list(parts)}. Ask me what I did to hear each one, or say undo."
        with self._sessions.begin() as s:
            key = now.strftime("activity-%G-W%V")
            if (
                s.scalars(
                    select(MemoRow).where(MemoRow.kind == "report", MemoRow.key == key)
                ).first()
                is None
            ):
                s.add(MemoRow(kind="report", key=key, text=text, created_at=now))
        return text

    def undo(self, description: str) -> Undo:
        """Undo the most recent action in the last week that matches ``description``
        (or simply the most recent one when the description says nothing specific)."""
        wanted = keywords(description) - {"undo", "last", "that", "reverse", "back", "take"}
        since = self._clock() - timedelta(days=7)
        with self._sessions.begin() as s:
            rows = [r for r in self._executed(s, since) if not (r.result or "").endswith(UNDONE)]
            if wanted:
                rows = [
                    r
                    for r in rows
                    if wanted
                    & keywords(f"{r.readback} {r.result or ''} {(r.params or {}).get('title', '')}")
                ]
            if not rows:
                return Undo("I couldn't find that among this week's actions.")
            row = rows[0]
            return self._reverse(s, row)

    def _reverse(self, s: Session, row: PendingActionRow) -> Undo:
        params = row.params or {}
        if row.kind in _CANNOT_UNDO:
            return Undo(_CANNOT_UNDO[row.kind])
        if row.kind in (ActionKind.DEFER, ActionKind.DROP, ActionKind.CLOSE_TASK):
            item = s.get(ItemRow, row.item_id) if row.item_id else None
            if item is None:
                return Undo("That item is gone.")
            item.status = ItemStatus.OPEN
            item.deferred_until = None
            self._mark(s, row)
            audit(s, f"undo.{row.kind.value}", "ok", target=item.id, actor="ceo")
            note = " Reopen it in its own app too." if row.kind == ActionKind.CLOSE_TASK else ""
            return Undo(f"{item.title} is back on your list.{note}", done=True)
        if row.kind == ActionKind.ADD_CONTACT:
            person = Delegate.model_validate(params["delegate"])
            existing = s.get(DelegateRow, person.id)
            if existing is not None:
                s.delete(existing)
            self._mark(s, row)
            audit(s, "undo.add_contact", "ok", target=person.id, actor="ceo")
            return Undo(f"Removed {person.full_name} from your team again.", done=True)
        if row.kind == ActionKind.REMOVE_CONTACT:
            return Undo("Say add, with their name and email address, to put them back.")
        if row.kind == ActionKind.MOVE_MEETING:
            item = s.get(ItemRow, row.item_id) if row.item_id else None
            old_start, old_end = params.get("previous_start"), params.get("previous_end")
            if item is None or not old_start or not old_end:
                return Undo("I don't know the meeting's old time, so I can't move it back.")
            readback = f"Moving {item.title} back to its old time. Confirm?"
            return Undo(
                readback,
                propose=(item.id, readback, {"start": old_start, "end": old_end}),
            )
        return Undo("That can't be undone.")

    @staticmethod
    def _mark(s: Session, row: PendingActionRow) -> None:
        row.result = f"{row.result or ''} {UNDONE}".strip()
