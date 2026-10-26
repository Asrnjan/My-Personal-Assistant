"""Meeting notes: turn what the CEO dictates or pastes after a meeting into a short summary,
the decisions taken, and action items. Each action item with an owner on the team becomes
a task the conversation offers to delegate, so it is tracked and followed up like any other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, tzinfo

from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import ItemRow, new_id
from app.domain.enums import ItemKind, ItemStatus, PriorityBand, Source
from app.llm.client import LLMClient, Task
from app.llm.prompts import load_prompt, untrusted
from app.llm.schemas import NotesSummary

MAX_NOTES_CHARS = 20000


@dataclass
class NotedAction:
    item_id: str
    task: str
    owner: str | None
    due: date | None


class MeetingNotes:
    def __init__(
        self, session_factory: sessionmaker[Session], llm: LLMClient, tz: tzinfo, clock: Clock
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._tz = tz
        self._clock = clock

    def read(self, notes: str, meeting: str = "") -> NotesSummary:
        today = self._clock().astimezone(self._tz).date()
        header = f"Today is {today:%A %d %B %Y}."
        if meeting:
            header += f" The meeting: {meeting}."
        return self._llm.structured(
            Task.CONVERSATION,
            "meeting_notes",
            system=[load_prompt("notes").text],
            user=header + "\n" + untrusted("meeting notes", notes[:MAX_NOTES_CHARS]),
            schema=NotesSummary,
            max_tokens=2000,
        )

    def track(self, summary: NotesSummary, meeting: str = "") -> list[NotedAction]:
        """Store each action item as an open task so it can be delegated and followed up."""
        now = self._clock()
        out = []
        with self._sessions.begin() as s:
            for action in summary.actions:
                task = " ".join(action.task.split())[:300]
                if not task:
                    continue
                due = _date(action.due)
                row = ItemRow(
                    id=new_id(),
                    source=Source.NOTES,
                    source_id=new_id(),
                    kind=ItemKind.TASK,
                    title=task,
                    description=f"From the meeting notes{': ' + meeting if meeting else ''}.",
                    participants=[],
                    status=ItemStatus.OPEN,
                    # The CEO's own notes: already decided, so no scoring call is needed.
                    priority_band=PriorityBand.P2_DELEGATE if action.owner else None,
                    priority_score=60,
                    reason="An action item from your meeting notes.",
                    due=datetime.combine(due, datetime.min.time(), tzinfo=self._tz)
                    if due
                    else None,
                    scored_at=now,
                    last_synced_at=now,
                )
                s.add(row)
                out.append(NotedAction(row.id, task, action.owner, due))
        return out


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
