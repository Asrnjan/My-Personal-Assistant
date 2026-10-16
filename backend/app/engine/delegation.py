"""Delegation: resolve the spoken name, read back, draft the brief, dispatch it
on every preferred channel, update the source, and start a follow-up."""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal

import jellyfish
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from app.audit import audit
from app.clock import Clock
from app.db.models import DecisionRow, FollowUpRow, ItemRow
from app.domain.delegate import Delegate
from app.domain.enums import (
    Actor,
    ApprovedVia,
    Channel,
    DecisionAction,
    FollowUpState,
    ItemKind,
    ItemStatus,
    Source,
)
from app.engine.briefing import clean_title, spoken_list, spoken_time
from app.engine.contacts import all_delegates, display_names
from app.engine.recipients import RecipientGuard
from app.integrations.base import CalendarAdapter, TaskAdapter
from app.integrations.notify.base import DelegationMessage, Notifier
from app.integrations.registry import Adapters
from app.llm.client import LLMClient, Task
from app.llm.errors import BudgetExceededError, LLMError
from app.llm.prompts import load_prompt, untrusted
from app.llm.schemas import DelegationBrief

log = logging.getLogger(__name__)

MATCH_THRESHOLD = 80
# Candidates within this many points of the best are treated as equally likely.
TIE_MARGIN = 6
# A delegated deadline given as a date means the end of that working day.
DEADLINE_TIME = time(18, 0)

HandoffType = Literal["full_ownership", "keep_me_updated"]
NotifiersFactory = Callable[[Adapters], list[Notifier]]

_CHANNEL_WORDS = {
    Channel.EMAIL: "email",
    Channel.TELEGRAM: "Telegram",
    Channel.SLACK: "Slack",
    Channel.TEAMS: "Teams",
}


# --- name resolution ------------------------------------------------------------


@dataclass(frozen=True)
class NameMatch:
    delegate: Delegate | None
    candidates: list[Delegate] = field(default_factory=list)
    question: str | None = None

    @property
    def ok(self) -> bool:
        return self.delegate is not None


def _normalize(name: str) -> str:
    text = re.sub(r"[^\w\s@.'-]", " ", name.lower())
    text = re.sub(r"^\s*(to|for|with)\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _phonetic_match(a: str, b: str) -> bool:
    if len(a) < 3 or len(b) < 3:
        return False
    return jellyfish.soundex(a) == jellyfish.soundex(b) or jellyfish.metaphone(
        a
    ) == jellyfish.metaphone(b)


def _name_score(spoken: str, delegate: Delegate) -> float:
    names = [delegate.full_name, *delegate.aliases, delegate.id.replace("-", " ")]
    best = max(fuzz.WRatio(spoken, _normalize(n)) for n in names if n)
    # Speech-to-text often gets the spelling wrong but the sound right ("Raul" for "Rahul").
    tokens = spoken.split()
    name_tokens = {t for n in names for t in _normalize(n).split()}
    if tokens and all(any(_phonetic_match(t, nt) for nt in name_tokens) for t in tokens):
        best = max(best, 90.0)
    return float(best)


def _owns(delegate: Delegate, context: str) -> bool:
    words = set(re.findall(r"[a-z]+", context.lower()))
    for area in delegate.ownership_areas:
        area_words = set(re.findall(r"[a-z]+", area.lower()))
        if area_words and area_words <= words:
            return True
        if any(w in words for w in area_words if len(w) > 4):
            return True
    return False


class NameResolver:
    def __init__(self, delegates: Sequence[Delegate]) -> None:
        self._delegates = list(delegates)

    def resolve(self, spoken: str, context: str = "") -> NameMatch:
        """Match a spoken name against full names and aliases.

        When several people match about equally well, the one whose ownership
        areas fit ``context`` (the item) wins; otherwise the caller must ask.
        """
        query = _normalize(spoken)
        if not query:
            return NameMatch(None, question="Who should I give it to?")
        if "@" in query:
            # Addresses are accepted only when they belong to a known contact.
            exact = [d for d in self._delegates if str(d.email).lower() == query]
            if exact:
                return NameMatch(exact[0], exact)
            return self._unknown(spoken)
        # A full name or an alias said exactly settles it ("Rahul" is Rahul Kumar's alias).
        exact = [
            d
            for d in self._delegates
            if query in {_normalize(n) for n in [d.full_name, *d.aliases]}
        ]
        if len(exact) == 1:
            return NameMatch(exact[0], exact)
        scored = sorted(
            ((d, _name_score(query, d)) for d in self._delegates),
            key=lambda pair: pair[1],
            reverse=True,
        )
        if not scored or scored[0][1] < MATCH_THRESHOLD:
            return self._unknown(spoken)
        top = scored[0][1]
        close = [d for d, score in scored if score >= MATCH_THRESHOLD and top - score <= TIE_MARGIN]
        if len(close) == 1:
            return NameMatch(close[0], close)
        if context:
            owners = [d for d in close if _owns(d, context)]
            if len(owners) == 1:
                return NameMatch(owners[0], close)
        names = [d.full_name for d in close]
        if len(names) == 2:
            question = f"{names[0]} or {names[1]}?"
        else:
            question = f"{', '.join(names[:-1])}, or {names[-1]}?"
        return NameMatch(None, close, question)

    @staticmethod
    def _unknown(spoken: str) -> NameMatch:
        name = spoken.strip() or "that name"
        return NameMatch(
            None,
            question=(
                f"I don't have anyone called {name} in your contacts. "
                "Could you say the name again, or add them to your contacts first?"
            ),
        )


# --- wording ----------------------------------------------------------------------


def spoken_day(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if today < day < today + timedelta(days=7):
        return day.strftime("%A")
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def deadline_at(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, DEADLINE_TIME, tzinfo=tz)


def channel_phrase(channels: Sequence[Channel]) -> str:
    return spoken_list([_CHANNEL_WORDS[c] for c in channels])


def reachable_channels(delegate: Delegate, notifiers: Sequence[Notifier]) -> list[Channel]:
    """The delegate's preferred channels that are set up and have an address for them."""
    available = {n.channel for n in notifiers if n.can_reach(delegate)}
    return [c for c in delegate.preferred_channels if c in available]


def delegation_readback(
    name: str,
    item: ItemRow,
    due: date | None,
    channels: Sequence[Channel],
    tz: tzinfo,
    today: date,
) -> str:
    title = clean_title(item.title)
    due_part = f", due {spoken_day(due, today)}" if due else ""
    how = f"{',' if due_part else ''} by {channel_phrase(channels)}"
    if item.kind == ItemKind.MEETING:
        when = f" at {spoken_time(item.start, tz)}" if item.start else ""
        return (
            f"Asking {name} to cover {title}{when} for you{how}, "
            "and declining it on your calendar. Confirm?"
        )
    return f"Sending {name} {title}{due_part}{how}. Confirm?"


def template_brief(
    item: ItemRow, first_name: str, deadline: str | None, handoff: HandoffType, note: str
) -> DelegationBrief:
    """Used when the model can't be reached: plain, but complete."""
    summary = f"Please take over {clean_title(item.title)}."
    if note:
        summary += f" {note.strip().rstrip('.')}."
    return DelegationBrief(
        subject=clean_title(item.title),
        task_summary=f"Hi {first_name}, {summary[0].lower()}{summary[1:]}",
        why_it_matters=item.reason or "It needs an owner and you're the right person for it.",
        expected_outcome="It's handled, and you tell me when it's done.",
        deadline=deadline,
        context_links=[],
        handoff_type=handoff,
    )


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    text: str
    html: str


def render_message(
    brief: DelegationBrief, links: Sequence[str], first_name: str, followup_ref: str
) -> RenderedMessage:
    subject = f"Delegated: {brief.subject} [ref {followup_ref}]"
    handoff = (
        "You own this from here."
        if brief.handoff_type == "full_ownership"
        else "Please keep me posted as you go."
    )
    lines = [
        f"Hi {first_name},",
        "",
        brief.task_summary,
        "",
        f"Why it matters: {brief.why_it_matters}",
        f"Expected outcome: {brief.expected_outcome}",
    ]
    if brief.deadline:
        lines.append(f"Deadline: {brief.deadline}")
    lines += [f"Handoff: {handoff}"]
    if links:
        lines += ["", "Links:", *(f"- {link}" for link in links)]
    lines += ["", 'Please reply "Accepted" so I know you have it.', "", "Sent by my assistant"]
    text = "\n".join(lines)

    esc = html.escape
    rows = [
        ("Why it matters", brief.why_it_matters),
        ("Expected outcome", brief.expected_outcome),
        *([("Deadline", brief.deadline)] if brief.deadline else []),
        ("Handoff", handoff),
    ]
    link_html = "".join(
        f'<li><a href="{esc(link, quote=True)}">{esc(link)}</a></li>' for link in links
    )
    body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5">'
        f"<p>Hi {esc(first_name)},</p>"
        f"<p>{esc(brief.task_summary)}</p>"
        + "".join(f"<p><strong>{esc(k)}:</strong> {esc(v)}</p>" for k, v in rows)
        + (f"<p><strong>Links:</strong></p><ul>{link_html}</ul>" if links else "")
        + "<p>Please reply <strong>Accepted</strong> so I know you have it.</p>"
        '<p style="color:#666">Sent by my assistant</p></div>'
    )
    return RenderedMessage(subject, text, body)


# --- dispatch ---------------------------------------------------------------------


@dataclass
class DelegationOutcome:
    ok: bool
    message: str
    decision_id: str | None = None
    followup_id: str | None = None
    channels_used: list[Channel] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def calendar_for(adapters: Adapters, source: Source) -> CalendarAdapter | None:
    return next((a for a in adapters.calendars if a.source == source), None)


def tasks_for(adapters: Adapters, source: Source) -> TaskAdapter | None:
    return next((a for a in adapters.tasks if a.source == source), None)


def _platform_id(delegate: Delegate, source: Source) -> str | None:
    return {
        Source.JIRA: delegate.jira_account_id,
        Source.ASANA: delegate.asana_gid,
        Source.CLICKUP: delegate.clickup_id,
    }.get(source)


class DelegationService:
    def __init__(
        self,
        llm: LLMClient,
        adapters_factory: Callable[[], Adapters],
        notifiers_factory: NotifiersFactory,
        tz: tzinfo,
        clock: Clock,
    ) -> None:
        self._llm = llm
        self._adapters_factory = adapters_factory
        self._notifiers_factory = notifiers_factory
        self._tz = tz
        self._clock = clock

    def today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    def adapters(self) -> Adapters:
        return self._adapters_factory()

    def notifiers(self) -> tuple[Adapters, list[Notifier]]:
        adapters = self._adapters_factory()
        return adapters, self._notifiers_factory(adapters)

    def draft(
        self,
        item: ItemRow,
        delegate: Delegate,
        first_name: str,
        due: date | None,
        handoff: HandoffType,
        note: str,
    ) -> DelegationBrief:
        deadline = spoken_day(due, self.today()) if due else None
        meta = {
            "delegate_first_name": first_name,
            "delegate_ownership_areas": delegate.ownership_areas,
            "kind": item.kind.value,
            "deadline": deadline,
            "handoff_type": handoff,
            "start": item.start.astimezone(self._tz).isoformat() if item.start else None,
        }
        user = (
            f"Delegation details: {json.dumps(meta, sort_keys=True)}\n"
            f"CEO's note (trusted): {note or 'none'}\n\n"
            + untrusted(item.source.value, f"Title: {item.title}\n{item.description[:3000]}")
        )
        try:
            brief = self._llm.structured(
                Task.CONVERSATION,
                "delegation.brief",
                system=[load_prompt("delegation").text],
                user=user,
                schema=DelegationBrief,
                max_tokens=1500,
            )
        except (LLMError, BudgetExceededError) as exc:
            log.warning("delegation brief fell back to the template: %s", exc)
            return template_brief(item, first_name, deadline, handoff, note)
        # The CEO's chosen handoff and deadline win over the model's.
        return brief.model_copy(update={"handoff_type": handoff, "deadline": deadline})

    def dispatch(
        self,
        s: Session,
        item: ItemRow,
        delegate_id: str,
        *,
        due: date | None,
        handoff: HandoffType,
        note: str,
        via: ApprovedVia,
    ) -> DelegationOutcome:
        now = self._clock()
        delegates = all_delegates(s)
        delegate = next((d for d in delegates if d.id == delegate_id), None)
        if delegate is None:
            audit(s, "delegate", "failed", target=item.id, reason="unknown delegate")
            return DelegationOutcome(False, "That person is no longer in your contacts.")
        guard = RecipientGuard(delegates)
        if not guard.allowed(str(delegate.email), item):
            audit(s, "delegate", "blocked", target=item.id, recipient=str(delegate.email))
            return DelegationOutcome(False, "I can only send to people in your contacts.")
        name = display_names(delegates).get(delegate.id, delegate.full_name)
        first_name = delegate.full_name.split()[0]
        adapters, notifiers = self.notifiers()

        brief = self.draft(item, delegate, first_name, due, handoff, note)
        links = [item.url] if item.url else []
        brief = brief.model_copy(update={"context_links": links})
        decision = DecisionRow(
            item_id=item.id,
            action=DecisionAction.DELEGATE,
            actor=Actor.CEO,
            delegate_id=delegate.id,
            brief_text=brief.model_dump_json(),
            approved_via=via,
        )
        s.add(decision)
        s.flush()
        followup = FollowUpRow(
            decision_id=decision.id,
            due=deadline_at(due, self._tz) if due else item.due,
            state=FollowUpState.PENDING,
            sent_at=now,
            refs={},
            seen_replies=[],
        )
        s.add(followup)
        s.flush()
        message = render_message(brief, links, first_name, followup.id[:8])

        used: list[Channel] = []
        failures: list[str] = []
        refs: dict[str, object] = {}
        for channel in delegate.preferred_channels:
            notifier = next(
                (n for n in notifiers if n.channel == channel and n.can_reach(delegate)), None
            )
            word = _CHANNEL_WORDS[channel]
            if notifier is None:
                failures.append(f"{word} isn't set up for {name}")
                audit(
                    s,
                    f"delegate.{channel.value}",
                    "skipped",
                    target=delegate.id,
                    decision_id=decision.id,
                    reason="channel not configured or no address",
                )
                continue
            try:
                delivery = notifier.send_delegation(
                    DelegationMessage(
                        followup.id, delegate, message.subject, message.text, message.html
                    )
                )
            except Exception as exc:
                log.exception("delegation via %s failed", channel.value)
                failures.append(f"{word} failed")
                audit(
                    s,
                    f"delegate.{channel.value}",
                    "failed",
                    target=delegate.id,
                    decision_id=decision.id,
                    error=str(exc)[:500],
                )
                continue
            used.append(channel)
            refs[channel.value] = delivery.ref
            audit(
                s,
                f"delegate.{channel.value}",
                "ok",
                target=delegate.id,
                decision_id=decision.id,
            )

        source_notes = self._update_source(s, adapters, item, delegate, message.text, decision.id)
        followup.refs = refs
        decision.channels_used = [c.value for c in used]
        decision.executed_at = now
        title = clean_title(item.title)
        if used:
            item.status = ItemStatus.DELEGATED
            text = f"Done. {name} has {title} by {channel_phrase(used)}."
            if failures:
                text += f" {'; '.join(failures).capitalize()}, so I used the other channels."
            decision.result = text
            if failures:
                decision.error = "; ".join(failures)
        else:
            followup.state = FollowUpState.CLOSED
            text = (
                f"I couldn't reach {name}: {'; '.join(failures) or 'no channel is set up'}. "
                "Nothing was sent."
            )
            decision.error = text
        if source_notes:
            text += " " + " ".join(source_notes)
        audit(
            s,
            "delegate",
            "ok" if used else "failed",
            target=item.id,
            decision_id=decision.id,
            delegate=delegate.id,
            channels=[c.value for c in used],
        )
        return DelegationOutcome(
            bool(used), text, decision.id, followup.id if used else None, used, failures
        )

    def _update_source(
        self,
        s: Session,
        adapters: Adapters,
        item: ItemRow,
        delegate: Delegate,
        brief_text: str,
        decision_id: str,
    ) -> list[str]:
        """Reassign the task or hand the meeting over. Returns sentences for the CEO
        about anything that could not be done."""
        notes: list[str] = []
        if item.kind == ItemKind.TASK:
            tasks = tasks_for(adapters, item.source)
            platform_id = _platform_id(delegate, item.source)
            if tasks is None or platform_id is None:
                why = "not connected" if tasks is None else f"no {item.source.value} id"
                audit(
                    s,
                    "task.reassign",
                    "skipped",
                    target=item.id,
                    decision_id=decision_id,
                    reason=why,
                )
                notes.append(f"I couldn't reassign it in {item.source.value.capitalize()} ({why}).")
                return notes
            try:
                tasks.reassign(item.source_id, platform_id)
                tasks.comment(item.source_id, brief_text)
            except Exception as exc:
                audit(
                    s,
                    "task.reassign",
                    "failed",
                    target=item.id,
                    decision_id=decision_id,
                    error=str(exc)[:500],
                )
                notes.append(f"Reassigning it in {item.source.value.capitalize()} failed.")
            else:
                audit(
                    s,
                    "task.reassign",
                    "ok",
                    target=item.id,
                    decision_id=decision_id,
                    to=platform_id,
                )
        elif item.kind == ItemKind.MEETING:
            calendar = calendar_for(adapters, item.source)
            if calendar is None:
                audit(
                    s,
                    "meeting.forward",
                    "skipped",
                    target=item.id,
                    decision_id=decision_id,
                    reason="not connected",
                )
                notes.append("Your calendar isn't connected, so I couldn't forward the invite.")
                return notes
            try:
                calendar.add_attendee(item.source_id, str(delegate.email))
                calendar.decline(item.source_id, f"{delegate.full_name} will attend on my behalf.")
            except Exception as exc:
                audit(
                    s,
                    "meeting.forward",
                    "failed",
                    target=item.id,
                    decision_id=decision_id,
                    error=str(exc)[:500],
                )
                notes.append("Forwarding the invite failed.")
            else:
                audit(
                    s,
                    "meeting.forward",
                    "ok",
                    target=item.id,
                    decision_id=decision_id,
                    to=str(delegate.email),
                )
        return notes


__all__ = [
    "DelegationOutcome",
    "DelegationService",
    "NameMatch",
    "NameResolver",
    "calendar_for",
    "channel_phrase",
    "deadline_at",
    "delegation_readback",
    "reachable_channels",
    "render_message",
    "spoken_day",
    "tasks_for",
    "template_brief",
]
