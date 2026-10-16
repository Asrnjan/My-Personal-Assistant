"""Pending actions: proposed, read back, then confirmed or cancelled.

Every action that touches the outside world or the CEO's commitments goes
through here, so the approval policy, the decision record and the audit log
are applied in one place.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import audit
from app.clock import Clock
from app.db.models import DecisionRow, ItemRow, PendingActionRow
from app.domain.enums import (
    ActionKind,
    ActionStatus,
    Actor,
    ApprovedVia,
    DecisionAction,
    ItemKind,
    ItemStatus,
)
from app.engine.approval import ApprovalPolicy, Level
from app.engine.briefing import clean_title
from app.engine.delegation import DelegationService, calendar_for, tasks_for

log = logging.getLogger(__name__)

DEFAULT_DECLINE_NOTE = "Thanks for the invite. I can't make this one."

DecisionHook = Callable[[Session, ItemRow, PendingActionRow], None]


class PendingAction(BaseModel):
    id: str
    kind: ActionKind
    item_id: str
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
        title=item.title if item else "",
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
    ) -> None:
        self._sessions = session_factory
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
        item_id: str,
        readback: str,
        params: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> PendingAction:
        if self._policy.level(kind.value) == Level.NEVER:
            raise ActionError(self._policy.refusal)
        with self._sessions.begin() as s:
            item = s.get(ItemRow, item_id)
            if item is None:
                raise ActionError("item not found")
            # A new proposal for the same item and kind replaces the older one.
            for old in s.scalars(
                select(PendingActionRow).where(
                    PendingActionRow.item_id == item_id,
                    PendingActionRow.kind == kind,
                    PendingActionRow.status == ActionStatus.PENDING,
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
            stmt = select(PendingActionRow, ItemRow).join(
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

    def cancel(self, action_id: str) -> PendingAction:
        with self._sessions.begin() as s:
            row = s.get(PendingActionRow, action_id)
            if row is None:
                raise ActionError("action not found")
            if row.status == ActionStatus.PENDING:
                row.status = ActionStatus.CANCELLED
                row.resolved_at = self._clock()
                row.result = "Cancelled."
                audit(s, f"action.{row.kind.value}", "cancelled", target=row.item_id, actor="ceo")
            return _view(row, s.get(ItemRow, row.item_id))

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
            item = s.get(ItemRow, row.item_id)
            if item is None:  # pragma: no cover - guarded by the foreign key
                raise ActionError("item not found")
            try:
                outcome = self._execute(s, row, item, via)
            except Exception as exc:
                log.exception("action %s failed", row.id)
                outcome = _Outcome(False, f"That didn't work: {str(exc)[:200]}")
            row.status = ActionStatus.EXECUTED if outcome.ok else ActionStatus.FAILED
            row.resolved_at = self._clock()
            row.result = outcome.message
            row.decision_id = outcome.decision_id
            if outcome.ok and self._on_decision is not None:
                self._on_decision(s, item, row)
            return ActionResult(action_id=row.id, ok=outcome.ok, message=outcome.message)

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
