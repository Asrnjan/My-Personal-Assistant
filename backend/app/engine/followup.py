"""Follow-ups on delegations: acknowledgements, questions, nudges, overdue."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import audit
from app.clock import Clock
from app.db.models import DecisionRow, FollowUpRow, ItemRow
from app.domain.delegate import Delegate
from app.domain.enums import Channel, FollowUpState, ItemStatus
from app.engine.approval import ApprovalPolicy, Level
from app.engine.briefing import clean_title
from app.engine.contacts import all_delegates, display_names
from app.engine.delegation import spoken_day
from app.engine.notices import Notices
from app.integrations.notify.base import AckEvent, AckSignal, Notifier
from app.integrations.registry import Adapters
from app.llm.client import LLMClient, Task
from app.llm.errors import LLMError
from app.llm.prompts import load_prompt, untrusted
from app.llm.schemas import AckClassification

log = logging.getLogger(__name__)

NUDGE_BEFORE = timedelta(hours=24)
# Work with no deadline that nobody acknowledged gets a nudge after this long, and a
# second one after as long again; never more than MAX_NUDGES.
NUDGE_UNDATED_AFTER = timedelta(hours=48)
MAX_NUDGES = 2
_OPEN = (FollowUpState.PENDING, FollowUpState.NUDGED, FollowUpState.OVERDUE)
_ACK_WORDS = re.compile(
    r"\b(accept(ed)?|ok(ay)?|on it|will do|got it|sure|happy to|i'?ll (do|handle|take))\b",
    re.IGNORECASE,
)


class FollowUpView(BaseModel):
    id: str
    item_id: str
    title: str
    delegate_id: str | None
    delegate_name: str | None
    state: FollowUpState
    due: datetime | None
    acknowledged_at: datetime | None
    nudges_sent: int
    channels: list[str]


@dataclass
class FollowUpReport:
    acknowledged: int = 0
    questions: int = 0
    nudged: int = 0
    overdue: int = 0
    checked_in: int = 0


class FollowUpService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LLMClient,
        adapters_factory: Callable[[], Adapters],
        notifiers_factory: Callable[[Adapters], list[Notifier]],
        policy: ApprovalPolicy,
        notices: Notices,
        tz: tzinfo,
        clock: Clock,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._adapters_factory = adapters_factory
        self._notifiers_factory = notifiers_factory
        self._policy = policy
        self._notices = notices
        self._tz = tz
        self._clock = clock

    # -- views --------------------------------------------------------------------

    def views(self, open_only: bool = False) -> list[FollowUpView]:
        with self._sessions() as s:
            names = display_names(all_delegates(s))
            stmt = (
                select(FollowUpRow, DecisionRow, ItemRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .join(ItemRow, DecisionRow.item_id == ItemRow.id)
                .order_by(FollowUpRow.created_at.desc())
            )
            if open_only:
                stmt = stmt.where(FollowUpRow.state.in_(_OPEN))
            return [
                FollowUpView(
                    id=fu.id,
                    item_id=item.id,
                    title=item.title,
                    delegate_id=dec.delegate_id,
                    delegate_name=names.get(dec.delegate_id or ""),
                    state=fu.state,
                    due=fu.due,
                    acknowledged_at=fu.acknowledged_at,
                    nudges_sent=fu.nudges_sent,
                    channels=list(dec.channels_used or []),
                )
                for fu, dec, item in s.execute(stmt).all()
            ]

    # -- acknowledgement ----------------------------------------------------------

    def _load(
        self, s: Session, followup_id: str
    ) -> tuple[FollowUpRow, DecisionRow, ItemRow, Delegate | None] | None:
        fu = s.get(FollowUpRow, followup_id)
        if fu is None:
            return None
        dec = s.get(DecisionRow, fu.decision_id)
        if dec is None:  # pragma: no cover - guarded by the foreign key
            return None
        item = s.get(ItemRow, dec.item_id)
        if item is None:  # pragma: no cover - guarded by the foreign key
            return None
        delegate = next((d for d in all_delegates(s) if d.id == dec.delegate_id), None)
        return fu, dec, item, delegate

    def acknowledge(
        self,
        followup_id: str,
        via: str,
        *,
        sender_check: Callable[[Delegate], bool] | None = None,
    ) -> bool:
        """Mark a delegation as accepted. ``sender_check`` lets a channel verify that
        the person acknowledging really is the delegate (e.g. their Telegram chat id)."""
        with self._sessions.begin() as s:
            loaded = self._load(s, followup_id)
            if loaded is None:
                return False
            fu, dec, item, delegate = loaded
            if delegate is None or (sender_check is not None and not sender_check(delegate)):
                audit(s, "followup.ack", "rejected", target=followup_id, via=via)
                return False
            if fu.state not in _OPEN:
                return fu.state == FollowUpState.ACKNOWLEDGED
            fu.state = FollowUpState.ACKNOWLEDGED
            fu.acknowledged_at = self._clock()
            audit(s, "followup.ack", "ok", target=followup_id, decision_id=dec.id, via=via)
            name = display_names(all_delegates(s)).get(delegate.id, delegate.full_name)
            title = clean_title(item.title)
        self._notices.tell(f"{name} accepted {title}.", direct=False)
        return True

    def is_delegate(self, followup_id: str, sender_check: Callable[[Delegate], bool]) -> bool:
        """True when ``sender_check`` accepts the delegate of this follow-up."""
        with self._sessions() as s:
            loaded = self._load(s, followup_id)
            return loaded is not None and loaded[3] is not None and sender_check(loaded[3])

    def question(
        self,
        followup_id: str,
        via: str,
        text: str = "",
        *,
        sender_check: Callable[[Delegate], bool] | None = None,
    ) -> bool:
        with self._sessions.begin() as s:
            loaded = self._load(s, followup_id)
            if loaded is None:
                return False
            _fu, dec, item, delegate = loaded
            if delegate is None or (sender_check is not None and not sender_check(delegate)):
                audit(s, "followup.question", "rejected", target=followup_id, via=via)
                return False
            audit(s, "followup.question", "ok", target=followup_id, decision_id=dec.id, via=via)
            name = display_names(all_delegates(s)).get(delegate.id, delegate.full_name)
            title = clean_title(item.title)
        detail = f' They wrote: "{text[:200]}"' if text else ""
        self._notices.tell(f"{name} has a question about {title}.{detail}")
        return True

    def classify_reply(self, text: str) -> AckClassification:
        try:
            return self._llm.structured(
                Task.BULK,
                "followup.ack",
                system=[load_prompt("ack").text],
                user=untrusted("email_reply", text[:2000]),
                schema=AckClassification,
                max_tokens=200,
            )
        except LLMError as exc:
            log.warning("ack classification fell back to keywords: %s", exc)
            accepted = bool(_ACK_WORDS.search(text))
            return AckClassification(
                acknowledged=accepted, has_question="?" in text, confidence=0.5
            )

    def check_replies(self) -> FollowUpReport:
        """Read replies on email threads and other channels that support polling."""
        report = FollowUpReport()
        adapters = self._adapters_factory()
        notifiers = self._notifiers_factory(adapters)
        events: list[tuple[str, str, AckEvent]] = []
        with self._sessions.begin() as s:
            delegates = {d.id: d for d in all_delegates(s)}
            rows = s.execute(
                select(FollowUpRow, DecisionRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .where(FollowUpRow.state.in_(_OPEN))
            ).all()
            for fu, dec in rows:
                delegate = delegates.get(dec.delegate_id or "")
                if delegate is None:
                    continue
                seen = set(fu.seen_replies or [])
                for event in self._email_events(adapters, fu, delegate, seen):
                    events.append((fu.id, "email", event))
                for notifier in notifiers:
                    ref = (fu.refs or {}).get(notifier.channel.value)
                    if notifier.channel == Channel.EMAIL or not ref:
                        continue
                    try:
                        found = notifier.poll_ack(ref, seen)
                    except Exception:
                        log.exception("polling %s for acknowledgements failed", notifier.channel)
                        continue
                    events.extend((fu.id, notifier.channel.value, e) for e in found)
                new_seen = [e.reply_id for fid, _c, e in events if fid == fu.id and e.reply_id]
                if new_seen:
                    fu.seen_replies = [*seen, *new_seen]
        for followup_id, via, event in events:
            if event.signal == "acknowledged":
                report.acknowledged += self.acknowledge(followup_id, via)
            else:
                report.questions += self.question(followup_id, via, event.text)
        return report

    def _email_events(
        self, adapters: Adapters, fu: FollowUpRow, delegate: Delegate, seen: set[str]
    ) -> list[AckEvent]:
        ref: dict[str, Any] = (fu.refs or {}).get("email") or {}
        thread_id = ref.get("thread_id")
        if not thread_id:
            return []
        mail = next((m for m in adapters.mail if m.source.value == ref.get("source")), None)
        if mail is None:
            return []
        since = fu.sent_at or fu.created_at
        try:
            replies = mail.list_thread_replies(str(thread_id), since)
        except Exception:
            log.exception("reading replies for follow-up %s failed", fu.id)
            return []
        events = []
        for reply in replies:
            if reply.id in seen:
                continue
            # Only the delegate's own replies count.
            if (reply.sender or "").lower() != str(delegate.email).lower():
                continue
            verdict = self.classify_reply(f"Subject: {reply.subject}\n{reply.snippet}")
            # A reply that isn't a clear yes is passed to the CEO as a question.
            accepted = verdict.acknowledged and verdict.confidence >= 0.5
            signal: AckSignal = "acknowledged" if accepted else "question"
            events.append(AckEvent(signal, reply.snippet, reply.id))
        return events

    # -- nudges and deadlines -----------------------------------------------------

    def nudge_due(self) -> int:
        """One reminder 24 hours before the deadline, if nobody acknowledged."""
        now = self._clock()
        level = self._policy.level("nudge_delegate")
        if level == Level.NEVER:
            return 0
        adapters = self._adapters_factory()
        notifiers = self._notifiers_factory(adapters)
        sent = 0
        tell: list[str] = []
        with self._sessions.begin() as s:
            delegates = {d.id: d for d in all_delegates(s)}
            names = display_names(list(delegates.values()))
            rows = s.execute(
                select(FollowUpRow, DecisionRow, ItemRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .join(ItemRow, DecisionRow.item_id == ItemRow.id)
                .where(
                    FollowUpRow.state == FollowUpState.PENDING,
                    FollowUpRow.due.is_not(None),
                    FollowUpRow.last_nudge_at.is_(None),
                )
            ).all()
            for fu, dec, item in rows:
                if fu.due is None or not (fu.due - NUDGE_BEFORE <= now < fu.due):
                    continue
                delegate = delegates.get(dec.delegate_id or "")
                if delegate is None:
                    continue
                title = clean_title(item.title)
                today = now.astimezone(self._tz).date()
                when = spoken_day(fu.due.astimezone(self._tz).date(), today)
                fu.last_nudge_at = now
                if level != Level.AUTO:
                    name = names.get(delegate.id, delegate.full_name)
                    tell.append(f"{name} hasn't acknowledged {title}, due {when}.")
                    continue
                first = delegate.full_name.split()[0]
                text = (
                    f'Hi {first}, a quick reminder about "{title}", due {when}. '
                    "Can you confirm you have it?"
                )
                delivered = []
                for notifier in notifiers:
                    ref = (fu.refs or {}).get(notifier.channel.value)
                    if ref is None:
                        continue
                    try:
                        notifier.send_nudge(delegate, text, ref)
                    except Exception as exc:
                        audit(
                            s,
                            f"nudge.{notifier.channel.value}",
                            "failed",
                            target=fu.id,
                            error=str(exc)[:300],
                        )
                        continue
                    delivered.append(notifier.channel.value)
                    audit(
                        s, f"nudge.{notifier.channel.value}", "ok", target=fu.id, decision_id=dec.id
                    )
                if delivered:
                    fu.state = FollowUpState.NUDGED
                    fu.nudges_sent += 1
                    sent += 1
        for text in tell:
            self._notices.tell(text)
        return sent

    def _send(
        self,
        s: Session,
        notifiers: list[Notifier],
        fu: FollowUpRow,
        dec: DecisionRow,
        delegate: Delegate,
        text: str,
        kind: str,
    ) -> bool:
        """Send ``text`` to the delegate on every channel the delegation went out on."""
        delivered = False
        for notifier in notifiers:
            ref = (fu.refs or {}).get(notifier.channel.value)
            if ref is None:
                continue
            try:
                notifier.send_nudge(delegate, text, ref)
            except Exception as exc:
                audit(
                    s,
                    f"{kind}.{notifier.channel.value}",
                    "failed",
                    target=fu.id,
                    error=str(exc)[:300],
                )
                continue
            delivered = True
            audit(s, f"{kind}.{notifier.channel.value}", "ok", target=fu.id, decision_id=dec.id)
        return delivered

    def renudge(self) -> int:
        """Chase work nobody acknowledged: undated work after two days, and a second
        reminder two days after the first. The CEO isn't bothered unless it goes overdue."""
        now = self._clock()
        if self._policy.level("nudge_delegate") != Level.AUTO:
            return 0
        notifiers = self._notifiers_factory(self._adapters_factory())
        sent = 0
        with self._sessions.begin() as s:
            delegates = {d.id: d for d in all_delegates(s)}
            rows = s.execute(
                select(FollowUpRow, DecisionRow, ItemRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .join(ItemRow, DecisionRow.item_id == ItemRow.id)
                .where(FollowUpRow.state.in_([FollowUpState.PENDING, FollowUpState.NUDGED]))
            ).all()
            for fu, dec, item in rows:
                delegate = delegates.get(dec.delegate_id or "")
                if delegate is None or fu.nudges_sent >= MAX_NUDGES:
                    continue
                since = fu.last_nudge_at or fu.sent_at or fu.created_at
                undated_first = fu.due is None and fu.last_nudge_at is None
                again = fu.last_nudge_at is not None
                if not (undated_first or again) or now - since < NUDGE_UNDATED_AFTER:
                    continue
                first = delegate.full_name.split()[0]
                text = (
                    f'Hi {first}, just checking you saw "{clean_title(item.title)}". '
                    "Can you confirm you have it?"
                )
                fu.last_nudge_at = now
                if self._send(s, notifiers, fu, dec, delegate, text, "nudge"):
                    fu.state = FollowUpState.NUDGED
                    fu.nudges_sent += 1
                    sent += 1
        return sent

    def check_in(self) -> int:
        """Ask for a status on accepted work a day before it's due, and once more if it
        passes its deadline without being finished (telling the CEO only then)."""
        now = self._clock()
        if self._policy.level("nudge_delegate") != Level.AUTO:
            return 0
        notifiers = self._notifiers_factory(self._adapters_factory())
        sent = 0
        tell: list[str] = []
        with self._sessions.begin() as s:
            delegates = {d.id: d for d in all_delegates(s)}
            names = display_names(list(delegates.values()))
            rows = s.execute(
                select(FollowUpRow, DecisionRow, ItemRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .join(ItemRow, DecisionRow.item_id == ItemRow.id)
                .where(
                    FollowUpRow.state == FollowUpState.ACKNOWLEDGED, FollowUpRow.due.is_not(None)
                )
            ).all()
            today = now.astimezone(self._tz).date()
            for fu, dec, item in rows:
                delegate = delegates.get(dec.delegate_id or "")
                if delegate is None or fu.due is None:
                    continue
                last = fu.last_nudge_at
                title = clean_title(item.title)
                first = delegate.full_name.split()[0]
                when = spoken_day(fu.due.astimezone(self._tz).date(), today)
                if now >= fu.due and (last is None or last < fu.due):
                    text = f'Hi {first}, "{title}" was due {when}. Where are you with it?'
                    name = names.get(delegate.id, delegate.full_name)
                    tell.append(
                        f"{title} from {name} is past its deadline; I've asked for an update."
                    )
                elif fu.due - NUDGE_BEFORE <= now < fu.due and (
                    last is None or (fu.acknowledged_at and last < fu.acknowledged_at)
                ):
                    text = f'Hi {first}, how is "{title}" going? It\'s due {when}.'
                else:
                    continue
                fu.last_nudge_at = now
                if self._send(s, notifiers, fu, dec, delegate, text, "checkin"):
                    sent += 1
        for text in tell:
            self._notices.tell(text)
        return sent

    def close_finished(self) -> int:
        """Delegations whose work is done (closed at its source, or by the CEO) are closed."""
        with self._sessions.begin() as s:
            rows = s.execute(
                select(FollowUpRow, DecisionRow, ItemRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .join(ItemRow, DecisionRow.item_id == ItemRow.id)
                .where(
                    FollowUpRow.state != FollowUpState.CLOSED,
                    ItemRow.status.in_([ItemStatus.DONE, ItemStatus.DROPPED]),
                )
            ).all()
            for fu, dec, _item in rows:
                fu.state = FollowUpState.CLOSED
                audit(s, "followup.done", "ok", target=fu.id, decision_id=dec.id)
            return len(rows)

    def finish(self, item_id: str) -> str | None:
        """The CEO says delegated work is done. Returns its title, or None."""
        with self._sessions.begin() as s:
            item = s.get(ItemRow, item_id)
            if item is None:
                return None
            item.status = ItemStatus.DONE
            title = clean_title(item.title)
        self.close_finished()
        return title

    def mark_overdue(self) -> int:
        now = self._clock()
        tell: list[str] = []
        with self._sessions.begin() as s:
            names = display_names(all_delegates(s))
            rows = s.execute(
                select(FollowUpRow, DecisionRow, ItemRow)
                .join(DecisionRow, FollowUpRow.decision_id == DecisionRow.id)
                .join(ItemRow, DecisionRow.item_id == ItemRow.id)
                .where(
                    FollowUpRow.state.in_([FollowUpState.PENDING, FollowUpState.NUDGED]),
                    FollowUpRow.due.is_not(None),
                    FollowUpRow.due < now,
                )
            ).all()
            for fu, dec, item in rows:
                fu.state = FollowUpState.OVERDUE
                audit(s, "followup.overdue", "ok", target=fu.id, decision_id=dec.id)
                name = names.get(dec.delegate_id or "", "The delegate")
                tell.append(
                    f"{name} never acknowledged {clean_title(item.title)}, and it's now overdue."
                )
        for text in tell:
            self._notices.tell(text)
        return len(tell)

    def run(self) -> FollowUpReport:
        self.close_finished()
        report = self.check_replies()
        report.nudged = self.nudge_due() + self.renudge()
        report.overdue = self.mark_overdue()
        report.checked_in = self.check_in()
        return report
