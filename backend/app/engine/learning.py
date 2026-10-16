"""Learning from the CEO: explicit preferences and overrides of the assistant's
suggestions, folded back into the scoring prompt as a "CEO preferences" block."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import audit
from app.clock import Clock
from app.db.models import DelegateRow, ItemRow, LearningSignalRow, PendingActionRow
from app.domain.enums import ActionKind, PriorityBand, SignalKind
from app.engine.briefing import clean_title

MAX_RULES = 20
MAX_OVERRIDES = 15

_BAND_WORDS = {
    PriorityBand.P1_DO_MYSELF: "P1 (do it myself)",
    PriorityBand.P2_DELEGATE: "P2 (delegate)",
    PriorityBand.P3_DECLINE: "P3 (decline)",
    PriorityBand.P4_DROP: "P4 (drop)",
}


class Preference(BaseModel):
    id: str
    kind: SignalKind
    rule_text: str
    item_id: str | None
    created_at: datetime
    active: bool


def _view(row: LearningSignalRow) -> Preference:
    return Preference(
        id=row.id,
        kind=row.kind,
        rule_text=row.rule_text,
        item_id=row.item_id,
        created_at=row.created_at,
        active=row.active,
    )


def _describe(item: ItemRow) -> str:
    who = f" from {item.requester}" if item.requester else ""
    return f'"{clean_title(item.title)}"{who}'


def preferences_block(s: Session) -> list[str]:
    """The CEO's preferences as short lines for the scoring and conversation prompts.

    Explicit rules come first, then a count of how often the CEO lowered or
    raised items from the same requester, then the most recent overrides.
    """
    rows = list(
        s.scalars(
            select(LearningSignalRow)
            .where(LearningSignalRow.active.is_(True))
            .order_by(LearningSignalRow.created_at.desc(), LearningSignalRow.id)
        )
    )
    rules = [r.rule_text for r in rows if r.kind == SignalKind.RULE][:MAX_RULES]
    overrides = [r for r in rows if r.kind != SignalKind.RULE]
    lines: list[str] = []
    seen: set[str] = set()
    for text in rules:
        if text.lower() not in seen:
            seen.add(text.lower())
            lines.append(text)
    trend: Counter[tuple[str, str]] = Counter()
    for r in overrides:
        requester = (r.data or {}).get("requester")
        direction = (r.data or {}).get("direction")
        if requester and direction:
            trend[(str(requester), str(direction))] += 1
    for (requester, direction), count in sorted(trend.items()):
        if count >= 2:
            lines.append(f"The CEO has {direction} the priority of {count} items from {requester}.")
    lines += [r.rule_text for r in overrides[:MAX_OVERRIDES]]
    return lines


class LearningService:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._sessions = session_factory
        self._clock = clock

    def views(self) -> list[Preference]:
        with self._sessions() as s:
            return [
                _view(r)
                for r in s.scalars(
                    select(LearningSignalRow).order_by(LearningSignalRow.created_at.desc())
                )
            ]

    def block(self) -> list[str]:
        with self._sessions() as s:
            return preferences_block(s)

    def add_rule(self, text: str) -> Preference:
        rule = " ".join(text.split()).strip()
        if not rule:
            raise ValueError("a preference needs some text")
        with self._sessions.begin() as s:
            row = LearningSignalRow(
                kind=SignalKind.RULE, rule_text=rule[:500], data={}, created_at=self._clock()
            )
            s.add(row)
            s.flush()
            audit(s, "preference.add", "ok", target=row.id, actor="ceo")
            return _view(row)

    def delete(self, preference_id: str) -> bool:
        with self._sessions.begin() as s:
            row = s.get(LearningSignalRow, preference_id)
            if row is None:
                return False
            s.delete(row)
            audit(s, "preference.delete", "ok", target=preference_id, actor="ceo")
            return True

    def _record(
        self,
        s: Session,
        kind: SignalKind,
        item: ItemRow,
        text: str,
        data: dict[str, Any],
    ) -> None:
        s.add(
            LearningSignalRow(
                kind=kind,
                item_id=item.id,
                rule_text=text,
                data={"requester": item.requester, **data},
                created_at=self._clock(),
            )
        )

    def set_band(self, item_id: str, band: PriorityBand) -> str:
        """The CEO says an item should be in another band: apply it and learn from it."""
        with self._sessions.begin() as s:
            item = s.get(ItemRow, item_id)
            if item is None:
                raise ValueError("item not found")
            old = item.priority_band
            title = clean_title(item.title)
            if old == band:
                return f"{title} is already {_BAND_WORDS[band]}."
            item.priority_band = band
            item.ceo_required = band == PriorityBand.P1_DO_MYSELF
            if band != PriorityBand.P2_DELEGATE:
                item.suggested_delegate_id = None
            self._record_band_change(s, item, old, band, "moved")
            audit(s, "item.set_band", "ok", target=item.id, actor="ceo", band=band.value)
            return f"Done. {title} is now {_BAND_WORDS[band]}."

    def _record_band_change(
        self,
        s: Session,
        item: ItemRow,
        old: PriorityBand | None,
        new: PriorityBand,
        verb: str,
    ) -> None:
        order = list(PriorityBand)
        if old is None:
            direction = "set"
        else:
            direction = "raised" if order.index(new) < order.index(old) else "lowered"
        was = f" from {_BAND_WORDS[old]}" if old else ""
        self._record(
            s,
            SignalKind.BAND_OVERRIDE,
            item,
            f"The CEO {verb} {_describe(item)}{was} to {_BAND_WORDS[new]}.",
            {"from": old.value if old else None, "to": new.value, "direction": direction},
        )

    def on_decision(self, s: Session, item: ItemRow, action: PendingActionRow) -> None:
        """Called after the CEO approves an action; records it when it overrides
        what the assistant suggested."""
        band = item.priority_band
        if action.kind == ActionKind.DELEGATE:
            chosen = str((action.params or {}).get("delegate_id") or "")
            if band is not None and band != PriorityBand.P2_DELEGATE:
                self._record_band_change(s, item, band, PriorityBand.P2_DELEGATE, "delegated")
            suggested = item.suggested_delegate_id
            if suggested and chosen and suggested != chosen:
                names = {d.id: d.full_name for d in s.scalars(select(DelegateRow))}
                self._record(
                    s,
                    SignalKind.DELEGATE_OVERRIDE,
                    item,
                    f"For work like {_describe(item)}, the CEO chose "
                    f"{names.get(chosen, chosen)} instead of {names.get(suggested, suggested)}.",
                    {"from": suggested, "to": chosen},
                )
        elif action.kind in (ActionKind.DECLINE_MEETING, ActionKind.DROP) and band in (
            PriorityBand.P1_DO_MYSELF,
            PriorityBand.P2_DELEGATE,
        ):
            target = (
                PriorityBand.P3_DECLINE
                if action.kind == ActionKind.DECLINE_MEETING
                else PriorityBand.P4_DROP
            )
            verb = "declined" if action.kind == ActionKind.DECLINE_MEETING else "dropped"
            self._record_band_change(s, item, band, target, verb)
