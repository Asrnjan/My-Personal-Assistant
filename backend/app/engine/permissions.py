"""Permission levels: which actions may run without a yes when everyone involved is on the
CEO's team. Anyone outside the team always gets a read-back and waits for a yes; the never
list (payments, deleting) is untouched by this.

Defaults come from ``approval_policy.yaml`` (``auto_when_team``); the CEO can change them by
voice, and those changes are kept in the database.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import MemoRow
from app.domain.enums import ActionKind
from app.engine.approval import ApprovalPolicy
from app.engine.contacts import all_delegates

Mode = Literal["ask", "team"]

TEAM_SCOPED: dict[ActionKind, str] = {
    ActionKind.SEND_EMAIL: "emails",
    ActionKind.REPLY_EMAIL: "email replies",
    ActionKind.TEAMS_MESSAGE: "Teams messages",
    ActionKind.BOOK_MEETING: "meetings",
    ActionKind.DELEGATE: "delegations",
}
KIND = "permission"


class Permissions:
    def __init__(
        self, session_factory: sessionmaker[Session], policy: ApprovalPolicy, clock: Clock
    ) -> None:
        self._sessions = session_factory
        self._policy = policy
        self._clock = clock

    def mode(self, kind: ActionKind) -> Mode:
        if kind not in TEAM_SCOPED:
            return "ask"
        with self._sessions() as s:
            row = s.scalars(
                select(MemoRow).where(MemoRow.kind == KIND, MemoRow.key == kind.value)
            ).first()
            if row is not None:
                return "team" if row.text == "team" else "ask"
        return "team" if kind.value in self._policy.auto_when_team else "ask"

    def set(self, kind: ActionKind, mode: Mode) -> None:
        if kind not in TEAM_SCOPED:
            raise ValueError(f"{kind.value} always needs a yes")
        with self._sessions.begin() as s:
            row = s.scalars(
                select(MemoRow).where(MemoRow.kind == KIND, MemoRow.key == kind.value)
            ).first()
            if row is None:
                s.add(MemoRow(kind=KIND, key=kind.value, text=mode, created_at=self._clock()))
            else:
                row.text = mode

    def describe(self) -> list[str]:
        out = []
        for kind, label in TEAM_SCOPED.items():
            if self.mode(kind) == "team":
                out.append(f"{label.capitalize()} to your team go without asking.")
            else:
                out.append(f"{label.capitalize()} always wait for your yes.")
        return out

    def allows(self, kind: ActionKind, params: dict[str, Any]) -> bool:
        """True when this action may run without asking: its kind is set to "team" and
        everyone it reaches is on the team."""
        if self.mode(kind) != "team":
            return False
        if kind == ActionKind.DELEGATE:
            return bool(params.get("delegate_id"))
        if kind in (ActionKind.SEND_EMAIL, ActionKind.REPLY_EMAIL):
            people = list(params.get("to") or [])
        elif kind == ActionKind.TEAMS_MESSAGE:
            people = [params["email"]] if params.get("email") else []
            if not people:
                return False  # a group chat: who is in it isn't checked here
        else:
            people = list(params.get("attendees") or [])
        with self._sessions() as s:
            team = {str(d.email).lower() for d in all_delegates(s)}
        return all(str(p).lower() in team for p in people)
