"""Pending actions: proposed, read back, then confirmed or cancelled.

Every action that touches the outside world or the CEO's commitments goes
through here, so the approval policy, the decision record and the audit log
are applied in one place.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.audit import audit
from app.clock import Clock
from app.db.models import DecisionRow, DelegateRow, ItemRow, PendingActionRow
from app.domain.delegate import Delegate
from app.domain.enums import (
    ActionKind,
    ActionStatus,
    Actor,
    ApprovedVia,
    DecisionAction,
    ItemKind,
    ItemStatus,
    Source,
)
from app.engine.approval import ApprovalPolicy, Level
from app.engine.briefing import clean_title
from app.engine.contacts import forget_contact, save_contact, writable_contacts
from app.engine.delegation import DelegationService, calendar_for, tasks_for
from app.integrations.base import NewEvent, OutboundEmail

log = logging.getLogger(__name__)

DEFAULT_DECLINE_NOTE = "Thanks for the invite. I can't make this one."
# The same email, message or booking executed again within this window is skipped.
DUPLICATE_WINDOW = timedelta(minutes=30)
# Fields that say what an action does; bookkeeping like the title shown in the app is ignored.
_SUBSTANCE = ("to", "subject", "text", "email", "chat_id", "start", "end", "attendees")


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in _SUBSTANCE)


DecisionHook = Callable[[Session, ItemRow, PendingActionRow], None]

# Things the CEO asks for in conversation that aren't about an item on the list.
FREE_KINDS = frozenset(
    {
        ActionKind.SEND_EMAIL,
        ActionKind.REPLY_EMAIL,
        ActionKind.TEAMS_MESSAGE,
        ActionKind.BOOK_MEETING,
        ActionKind.ADD_CONTACT,
        ActionKind.REMOVE_CONTACT,
    }
)


class PendingAction(BaseModel):
    id: str
    kind: ActionKind
    item_id: str | None
    title: str
    readback: str
    status: ActionStatus
    created_at: datetime
    result: str | None = None


class ActionResult(BaseModel):
    action_id: str
    ok: bool
    message: str


@dataclass
class _Outcome:
    ok: bool
    message: str
    decision_id: str | None = None


def _view(row: PendingActionRow, item: ItemRow | None) -> PendingAction:
    return PendingAction(
        id=row.id,
        kind=row.kind,
        item_id=row.item_id,
        title=item.title if item else str((row.params or {}).get("title", "")),
        readback=row.readback,
        status=row.status,
        created_at=row.created_at,
        result=row.result,
    )


class ActionError(ValueError):
    pass


class ActionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        delegation: DelegationService,
        policy: ApprovalPolicy,
        tz: tzinfo,
        clock: Clock,
        on_decision: DecisionHook | None = None,
        *,
        contacts_path: Path | None = None,
    ) -> None:
        self._sessions = session_factory
        # Team changes are written here too, so the next start doesn't undo them.
        self._contacts_path = contacts_path if writable_contacts(contacts_path) else None
        self._delegation = delegation
        self._policy = policy
        self._tz = tz
        self._clock = clock
        # Called after an action is executed (the learning engine records overrides).
        self._on_decision = on_decision

    def set_decision_hook(self, hook: DecisionHook) -> None:
        self._on_decision = hook

    # -- proposing ---------------------------------------------------------------

    def propose(
        self,
        kind: ActionKind,
        item_id: str | None,
        readback: str,
        params: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> PendingAction:
        if self._policy.level(kind.value) == Level.NEVER:
            raise ActionError(self._policy.refusal)
        if (item_id is None) != (kind in FREE_KINDS):
            raise ActionError(
                f"{kind.value} needs an item" if item_id is None else "no item needed"
            )
        with self._sessions.begin() as s:
            item = s.get(ItemRow, item_id) if item_id is not None else None
            if item_id is not None and item is None:
                raise ActionError("item not found")
            # A new proposal for the same item and kind replaces the older one.
            for old in (
                []
                if item_id is None
                else s.scalars(
                    select(PendingActionRow).where(
                        PendingActionRow.item_id == item_id,
                        PendingActionRow.kind == kind,
                        PendingActionRow.status == ActionStatus.PENDING,
                    )
                )
            ):
                old.status = ActionStatus.CANCELLED
                old.resolved_at = self._clock()
                old.result = "Replaced by a newer proposal."
            row = PendingActionRow(
                kind=kind,
                item_id=item_id,
                params=params or {},
                readback=readback,
                conversation_id=conversation_id,
                status=ActionStatus.PENDING,
                created_at=self._clock(),
            )
            s.add(row)
            s.flush()
            return _view(row, item)

    def pending(self, ids: Sequence[str] | None = None) -> list[PendingAction]:
        return self.find(ActionStatus.PENDING, ids)

    def find(
        self, status: ActionStatus | None = None, ids: Sequence[str] | None = None
    ) -> list[PendingAction]:
        with self._sessions() as s:
            stmt = select(PendingActionRow, ItemRow).outerjoin(
                ItemRow, PendingActionRow.item_id == ItemRow.id
            )
            if status is not None:
                stmt = stmt.where(PendingActionRow.status == status)
            if ids is not None:
                stmt = stmt.where(PendingActionRow.id.in_(list(ids)))
            rows = s.execute(stmt.order_by(PendingActionRow.created_at)).all()
            views = [_view(a, i) for a, i in rows]
        if ids is not None:
            order = {action_id: n for n, action_id in enumerate(ids)}
            views.sort(key=lambda v: order.get(v.id, len(order)))
        return views

    def get(self, action_id: str) -> PendingAction | None:
        found = self.find(None, [action_id])
        return found[0] if found else None

    # -- resolving ---------------------------------------------------------------

    def params(self, action_id: str) -> dict[str, Any]:
        with self._sessions() as s:
            row = s.get(PendingActionRow, action_id)
            return dict(row.params or {}) if row is not None else {}

    def cancel(self, action_id: str, reason: str = "Cancelled.") -> PendingAction:
        with self._sessions.begin() as s:
            row = s.get(PendingActionRow, action_id)
            if row is None:
                raise ActionError("action not found")
            if row.status == ActionStatus.PENDING:
                row.status = ActionStatus.CANCELLED
                row.resolved_at = self._clock()
                row.result = reason
                audit(
                    s,
                    f"action.{row.kind.value}",
                    "cancelled",
                    target=row.item_id or row.id,
                    actor="ceo",
                )
            return _view(row, s.get(ItemRow, row.item_id) if row.item_id else None)

    def confirm(self, action_id: str, via: ApprovedVia) -> ActionResult:
        with self._sessions.begin() as s:
            row = s.get(PendingActionRow, action_id)
            if row is None:
                raise ActionError("action not found")
            if row.status != ActionStatus.PENDING:
                return ActionResult(
                    action_id=row.id,
                    ok=row.status == ActionStatus.EXECUTED,
                    message=row.result or f"That was already {row.status.value}.",
                )
            item = s.get(ItemRow, row.item_id) if row.item_id else None
            if item is None and row.kind not in FREE_KINDS:  # pragma: no cover - foreign key
                raise ActionError("item not found")
            twin = self._just_done(s, row)
            if twin is not None:
                # The very same email or booking already went out: never do it twice.
                row.status = ActionStatus.CANCELLED
                row.resolved_at = self._clock()
                row.result = "Already done a moment ago, so I didn't do it again."
                audit(s, f"action.{row.kind.value}", "duplicate", target=row.id, twin=twin.id)
                return ActionResult(action_id=row.id, ok=True, message=row.result)
            try:
                if item is None:
                    outcome = self._execute_free(s, row)
                else:
                    outcome = self._execute(s, row, item, via)
            except Exception as exc:
                log.exception("action %s failed", row.id)
                outcome = _Outcome(False, f"That didn't work: {str(exc)[:200]}")
            row.status = ActionStatus.EXECUTED if outcome.ok else ActionStatus.FAILED
            row.resolved_at = self._clock()
            row.result = outcome.message
            row.decision_id = outcome.decision_id
            if outcome.ok and item is not None and self._on_decision is not None:
                self._on_decision(s, item, row)
            return ActionResult(action_id=row.id, ok=outcome.ok, message=outcome.message)

    def _just_done(self, s: Session, row: PendingActionRow) -> PendingActionRow | None:
        """An identical external action executed in the last half hour, if there is one."""
        if row.kind not in FREE_KINDS:
            return None
        since = self._clock() - DUPLICATE_WINDOW
        for done in s.scalars(
            select(PendingActionRow).where(
                PendingActionRow.kind == row.kind,
                PendingActionRow.status == ActionStatus.EXECUTED,
                PendingActionRow.resolved_at >= since,
                PendingActionRow.id != row.id,
            )
        ):
            if _same(done.params or {}, row.params or {}):
                return done
        return None

    def confirm_many(self, ids: Sequence[str], via: ApprovedVia) -> list[ActionResult]:
        return [self.confirm(i, via) for i in ids]

    # -- execution ---------------------------------------------------------------

    def _execute(
        self, s: Session, row: PendingActionRow, item: ItemRow, via: ApprovedVia
    ) -> _Outcome:
        params = row.params or {}
        if row.kind == ActionKind.DELEGATE:
            due = date.fromisoformat(params["due"]) if params.get("due") else None
            result = self._delegation.dispatch(
                s,
                item,
                str(params["delegate_id"]),
                due=due,
                handoff=params.get("handoff_type", "full_ownership"),
                note=str(params.get("note") or ""),
                via=via,
            )
            return _Outcome(result.ok, result.message, result.decision_id)
        if row.kind == ActionKind.DECLINE_MEETING:
            return self._decline(s, item, str(params.get("note") or DEFAULT_DECLINE_NOTE), via)
        if row.kind == ActionKind.CLOSE_TASK:
            return self._close(s, item, via)
        if row.kind == ActionKind.MOVE_MEETING:
            moved = dict(params)
            outcome = self._move(s, item, moved, via)
            row.params = moved  # a new dict, so the JSON column notices the change
            return outcome
        if row.kind == ActionKind.DEFER:
            until = date.fromisoformat(str(params["until"]))
            return self._local(
                s,
                item,
                DecisionAction.DEFER,
                ItemStatus.DEFERRED,
                via,
                f"Deferred {clean_title(item.title)} to {until.strftime('%A %d %B')}.",
                deferred_until=datetime.combine(until, time(8, 0), tzinfo=self._tz),
            )
        return self._local(
            s,
            item,
            DecisionAction.DROP,
            ItemStatus.DROPPED,
            via,
            f"Dropped {clean_title(item.title)}.",
        )

    def _add_contact(self, s: Session, params: dict[str, Any]) -> _Outcome:
        person = Delegate.model_validate(params["delegate"])
        if s.get(DelegateRow, person.id) is not None:
            return _Outcome(False, f"Someone on your team already has the id {person.id}.")
        s.add(DelegateRow(**person.model_dump(mode="json")))
        if self._contacts_path is not None:
            save_contact(self._contacts_path, person)
        audit(s, "delegate.create", "ok", target=person.id, actor="ceo", via="conversation")
        return _Outcome(True, f"Added {person.full_name} to your team.")

    def _remove_contact(self, s: Session, params: dict[str, Any]) -> _Outcome:
        delegate_id = str(params["delegate_id"])
        row = s.get(DelegateRow, delegate_id)
        if row is None:
            return _Outcome(False, "They're no longer on your team.")
        name = row.full_name
        # Past decisions keep their brief and the audit log keeps the id.
        s.execute(
            update(ItemRow)
            .where(ItemRow.suggested_delegate_id == delegate_id)
            .values(suggested_delegate_id=None)
        )
        s.execute(
            update(DecisionRow)
            .where(DecisionRow.delegate_id == delegate_id)
            .values(delegate_id=None)
        )
        s.delete(row)
        if self._contacts_path is not None:
            forget_contact(self._contacts_path, delegate_id)
        audit(s, "delegate.delete", "ok", target=delegate_id, actor="ceo", via="conversation")
        return _Outcome(True, f"Removed {name} from your team.")

    def _execute_free(self, s: Session, row: PendingActionRow) -> _Outcome:
        """Send an email or Teams message, book a meeting, or change the team, exactly as
        read back."""
        params = row.params or {}
        if row.kind == ActionKind.ADD_CONTACT:
            return self._add_contact(s, params)
        if row.kind == ActionKind.REMOVE_CONTACT:
            return self._remove_contact(s, params)
        adapters = self._delegation.adapters()
        source = Source(params["source"]) if params.get("source") else None
        if row.kind in (ActionKind.SEND_EMAIL, ActionKind.REPLY_EMAIL):
            mail = next((m for m in adapters.mail if source in (None, m.source)), None)
            if mail is None:
                return _Outcome(False, "Your email isn't connected, so I couldn't send it.")
            sent = mail.send(
                OutboundEmail(
                    to=tuple(params["to"]),
                    subject=str(params["subject"]),
                    text=str(params["text"]),
                    reply_to_message_id=params.get("message_id"),
                )
            )
            audit(
                s, f"email.{row.kind.value}", "ok", actor="ceo", to=params["to"], id=sent.message_id
            )
            verb = "Replied to" if row.kind == ActionKind.REPLY_EMAIL else "Sent the email to"
            return _Outcome(True, f"{verb} {params.get('to_name') or params['to'][0]}.")
        if row.kind == ActionKind.TEAMS_MESSAGE:
            if adapters.chat is None:
                return _Outcome(False, "Teams isn't connected, so I couldn't send it.")
            if params.get("chat_id"):
                adapters.chat.send(str(params["chat_id"]), str(params["text"]))
            else:
                adapters.chat.send_to_person(str(params["email"]), str(params["text"]))
            audit(
                s, "teams.message", "ok", actor="ceo", to=params.get("email") or params["chat_id"]
            )
            return _Outcome(True, f"Sent your Teams message to {params.get('to_name', 'them')}.")
        calendar = next((c for c in adapters.calendars if source in (None, c.source)), None)
        if calendar is None:
            return _Outcome(False, "Your calendar isn't connected, so I couldn't book it.")
        event = calendar.create_event(
            NewEvent(
                title=str(params["subject"]),
                start=datetime.fromisoformat(str(params["start"])),
                end=datetime.fromisoformat(str(params["end"])),
                attendees=tuple(params.get("attendees") or ()),
                online=bool(params.get("online", True)),
            )
        )
        audit(
            s, "meeting.book", "ok", actor="ceo", event=event.id, attendees=params.get("attendees")
        )
        return _Outcome(True, f"Booked {params['subject']} and sent the invites.")

    def _decision(
        self,
        s: Session,
        item: ItemRow,
        action: DecisionAction,
        via: ApprovedVia,
        result: str | None,
        error: str | None = None,
    ) -> DecisionRow:
        decision = DecisionRow(
            item_id=item.id,
            action=action,
            actor=Actor.CEO,
            approved_via=via,
            executed_at=self._clock(),
            result=result,
            error=error,
        )
        s.add(decision)
        s.flush()
        return decision

    def _decline(self, s: Session, item: ItemRow, note: str, via: ApprovedVia) -> _Outcome:
        title = clean_title(item.title)
        if item.kind != ItemKind.MEETING:
            return _Outcome(False, f"{title} isn't a meeting, so there's nothing to decline.")
        calendar = calendar_for(self._delegation.adapters(), item.source)
        if calendar is None:
            return _Outcome(False, "Your calendar isn't connected, so I couldn't decline it.")
        try:
            calendar.decline(item.source_id, note)
        except Exception as exc:
            decision = self._decision(s, item, DecisionAction.DECLINE, via, None, str(exc)[:500])
            audit(
                s,
                "meeting.decline",
                "failed",
                target=item.id,
                decision_id=decision.id,
                error=str(exc)[:500],
            )
            return _Outcome(False, f"Declining {title} failed.", decision.id)
        message = f"Declined {title} with a note to the organizer."
        decision = self._decision(s, item, DecisionAction.DECLINE, via, message)
        item.status = ItemStatus.DECLINED
        audit(s, "meeting.decline", "ok", target=item.id, decision_id=decision.id, note=note)
        return _Outcome(True, message, decision.id)

    def _move(
        self, s: Session, item: ItemRow, params: dict[str, Any], via: ApprovedVia
    ) -> _Outcome:
        title = clean_title(item.title)
        calendar = calendar_for(self._delegation.adapters(), item.source)
        if item.kind != ItemKind.MEETING or calendar is None:
            return _Outcome(False, f"I can't move {title} from here.")
        start = datetime.fromisoformat(str(params["start"]))
        end = datetime.fromisoformat(str(params["end"]))
        old = {"start": item.start.isoformat() if item.start else None}
        old["end"] = item.end.isoformat() if item.end else None
        try:
            calendar.move_event(item.source_id, start, end)
        except NotImplementedError:
            return _Outcome(False, f"Your calendar can't move {title}.")
        # Kept so the move can be undone.
        params.update(previous_start=old["start"], previous_end=old["end"])
        message = f"Moved {title}. The attendees have the new time."
        decision = self._decision(s, item, DecisionAction.DEFER, via, message)
        item.start, item.end = start, end
        audit(s, "meeting.move", "ok", target=item.id, decision_id=decision.id, previous=old)
        return _Outcome(True, message, decision.id)

    def _close(self, s: Session, item: ItemRow, via: ApprovedVia) -> _Outcome:
        title = clean_title(item.title)
        tasks = tasks_for(self._delegation.adapters(), item.source)
        if item.kind == ItemKind.TASK and tasks is not None:
            try:
                tasks.close(item.source_id)
            except Exception as exc:
                decision = self._decision(s, item, DecisionAction.CLOSE, via, None, str(exc)[:500])
                audit(
                    s,
                    "task.close",
                    "failed",
                    target=item.id,
                    decision_id=decision.id,
                    error=str(exc)[:500],
                )
                return _Outcome(False, f"Closing {title} failed.", decision.id)
            audit(s, "task.close", "ok", target=item.id)
        message = f"Closed {title}."
        decision = self._decision(s, item, DecisionAction.CLOSE, via, message)
        item.status = ItemStatus.DONE
        return _Outcome(True, message, decision.id)

    def _local(
        self,
        s: Session,
        item: ItemRow,
        action: DecisionAction,
        status: ItemStatus,
        via: ApprovedVia,
        message: str,
        deferred_until: datetime | None = None,
    ) -> _Outcome:
        decision = self._decision(s, item, action, via, message)
        item.status = status
        item.deferred_until = deferred_until
        audit(s, f"item.{action.value}", "ok", target=item.id, decision_id=decision.id)
        return _Outcome(True, message, decision.id)

    def reopen_deferred(self) -> int:
        """Deferred items come back once their date arrives."""
        now = self._clock()
        with self._sessions.begin() as s:
            rows = list(
                s.scalars(
                    select(ItemRow).where(
                        ItemRow.status == ItemStatus.DEFERRED,
                        ItemRow.deferred_until.is_not(None),
                        ItemRow.deferred_until <= now,
                    )
                )
            )
            for row in rows:
                row.status = ItemStatus.OPEN
                row.deferred_until = None
                row.scored_at = None
                row.alerted_hash = None
            return len(rows)
