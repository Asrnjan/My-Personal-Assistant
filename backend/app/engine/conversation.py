"""The voice-first conversation agent.

A turn goes through three layers:

1. Guards: financial or destructive requests are refused before anything else.
2. Dialogue rules handled in code, so they are instant, free and predictable:
   "brief me", "yes" / "go ahead", "go ahead with all", "cancel", "skip the
   second one".
3. Everything else goes to Sonnet with tools that map to engine functions.
   Actions with side effects only ever become pending actions; executing one
   needs the CEO's own "yes" in the current turn.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from anthropic.types import Message
from pydantic import BaseModel
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import audit
from app.clock import Clock
from app.db.models import ConversationRow, ItemRow
from app.domain.delegate import Delegate
from app.domain.enums import ActionKind, ApprovedVia, ItemKind, ItemStatus, PriorityBand, Source
from app.engine.actions import ActionError, ActionResult, ActionService, PendingAction
from app.engine.approval import ApprovalPolicy
from app.engine.briefing import Brief, Briefer, BriefItem, clean_title, spoken_time
from app.engine.contacts import all_delegates, display_names, new_contact_id
from app.engine.delegation import (
    DelegationService,
    NameResolver,
    delegation_readback,
    reachable_channels,
    spoken_day,
)
from app.engine.insights import first_free_slot
from app.engine.learning import LearningService, preferences_block
from app.engine.prep import upcoming_meeting
from app.engine.priority import prescore
from app.engine.reminders import unanswered_senders
from app.engine.rubric import Rubric
from app.engine.skills import SKILL_TOOLS, Skills
from app.integrations.base import Chat
from app.integrations.http import AuthExpiredError, IntegrationError
from app.llm.client import LLMClient, Task
from app.llm.errors import BudgetExceededError, LLMError
from app.llm.prompts import load_prompt, untrusted

log = logging.getLogger(__name__)

MAX_HISTORY = 12
MAX_TOOL_ROUNDS = 6
MAX_AGENDA = 30

# --- dialogue patterns --------------------------------------------------------------

_AFFIRM = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|correct|do it|go ahead|go for it|please do|"
    r"send it|sounds good|that's right|right|absolutely|affirmative)"
    r"([,\s]+(please|then|thanks|thank you|do it|go ahead|confirm))*[\s.!]*$",
    re.IGNORECASE,
)
_ALL = re.compile(
    r"\b(go ahead|confirm|do|yes|approve|send)\b.*\b(all|everything|them all|all of them|both)\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"^(no|nope|nah|cancel( (it|that|all|everything))?|stop|never ?mind|don't|do not|"
    r"forget it|hold off|wait)[\s.!]*$",
    re.IGNORECASE,
)
_SKIP = re.compile(r"\b(skip|cancel|drop|remove|not|except|leave out|don't do)\b", re.IGNORECASE)
_ORDINALS = {
    "first": 1,
    "1st": 1,
    "one": 1,
    "second": 2,
    "2nd": 2,
    "two": 2,
    "third": 3,
    "3rd": 3,
    "three": 3,
    "fourth": 4,
    "4th": 4,
    "four": 4,
    "fifth": 5,
    "5th": 5,
    "five": 5,
    "sixth": 6,
    "6th": 6,
    "six": 6,
    "last": -1,
}
_ORDINAL = re.compile(r"\b(" + "|".join(_ORDINALS) + r"|\d+)\b", re.IGNORECASE)
_BRIEF = re.compile(
    r"\b(brief( me)?|briefing|daily brief|my day|what'?s on|whats on|today look|"
    r"what do i have|wrap ?up|end of (the )?day|evening (brief|summary))\b",
    re.IGNORECASE,
)
_EVENING = re.compile(r"\b(wrap ?up|evening|end of (the )?day|tonight)\b", re.IGNORECASE)
_YES_WORDS = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|confirm|go ahead|do it|send it|approve)\b", re.IGNORECASE
)
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_ADDRESS = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[a-z]{2,}", re.IGNORECASE)

# Speech engines write a spoken address as words: "priya dot sharma at acme dot co dot in".
_DOMAIN_ENDINGS = (
    "com|org|net|io|co|in|ai|uk|us|de|fr|es|it|nl|au|ca|sg|ae|jp|edu|gov|info|biz|me|app|dev"
)
_SPOKEN_ADDRESS = re.compile(
    r"\b([a-z0-9][a-z0-9_%+-]*(?:\s*(?:\bdot\b|\.)\s*[a-z0-9_%+-]+)*)"
    r"\s+(?:at|@)\s+"
    r"([a-z0-9-]+(?:\s*(?:\bdot\b|\.)\s*[a-z0-9-]+)*\s*(?:\bdot\b|\.)\s*(?:"
    + _DOMAIN_ENDINGS
    + r"))\b",
    re.IGNORECASE,
)
_DOT = re.compile(r"\s*(?:\bdot\b|\.)\s*", re.IGNORECASE)


def spoken_addresses(text: str) -> str:
    """Turn "priya at acme dot com" into "priya@acme.com", leaving everything else alone."""

    def join(match: re.Match[str]) -> str:
        local = _DOT.sub(".", match.group(1)).lower()
        domain = _DOT.sub(".", match.group(2)).lower()
        return f"{local}@{domain}"

    return _SPOKEN_ADDRESS.sub(join, text)


def _and_list(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def is_affirmation(text: str) -> bool:
    return bool(_AFFIRM.match(text.strip()))


def spoken_reply(text: str) -> str:
    """Make model output safe to read aloud: no markdown, bullets or extra space."""
    out = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)
    out = re.sub(r"https?://\S+", "", out)
    out = re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", out, flags=re.MULTILINE)
    out = re.sub(r"[*_`#>|]", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _strip_confirm(readback: str) -> str:
    return re.sub(r"\s*Confirm\?\s*$", "", readback).rstrip(".")


_WORD = re.compile(r"[a-z0-9']{4,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _with_readbacks(reply: str, readbacks: list[str]) -> str:
    """Speak each new action's read-back once, in full, whatever the model said.

    The model is asked to say the read-back word for word, but it often rewords it or reads
    the email out itself, and adding the read-back after that read the message twice. When
    the model's text already covers most of a read-back, the read-back replaces it.
    """
    spoken = [re.sub(r"\s+", " ", r).strip() for r in readbacks]
    # A message without a full stop would run straight into "Confirm?" when spoken.
    spoken = [re.sub(r"([^.!?])\s+Confirm\?$", r"\1. Confirm?", r) for r in spoken]
    missing = [r for r in spoken if _strip_confirm(r) not in reply]
    if not missing:
        return reply
    said = _words(reply)
    for r in missing:
        wanted = _words(_strip_confirm(r))
        if wanted and len(wanted & said) >= len(wanted) / 2:
            return " ".join(spoken)
    return " ".join([reply, *missing]).strip()


# --- responses ------------------------------------------------------------------------


class ConverseResponse(BaseModel):
    conversation_id: str
    reply: str
    pending_actions: list[PendingAction]
    executed: list[ActionResult]
    needs_confirmation: bool
    brief: Brief | None = None
    # Accounts to connect (ids from GET /accounts) before the request can be done.
    needs_account: list[str] = []


@dataclass
class _Turn:
    text: str
    via: ApprovedVia
    created: list[str] = field(default_factory=list)
    executed: list[ActionResult] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    brief: Brief | None = None
    needs_account: list[str] = field(default_factory=list)


@dataclass
class _State:
    id: str
    history: list[dict[str, Any]]
    last_listed: list[str]
    awaiting: list[str]


# --- tool definitions ------------------------------------------------------------------

_ITEM_REF = {
    "type": "string",
    "description": "The item's id, or its number in the agenda such as '#2'.",
}
_DATE = {"type": "string", "description": "A calendar date, YYYY-MM-DD."}
_BANDS = [b.value for b in PriorityBand]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_items",
        "description": (
            "List open items (meetings, tasks, emails). Numbers them for later reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {**_DATE, "description": "Only items for this day (YYYY-MM-DD)."},
                "band": {"type": "string", "enum": _BANDS},
                "kind": {"type": "string", "enum": [k.value for k in ItemKind]},
            },
        },
    },
    {
        "name": "get_item",
        "description": "Details of one item.",
        "input_schema": {
            "type": "object",
            "properties": {"item_ref": _ITEM_REF},
            "required": ["item_ref"],
        },
    },
    {
        "name": "explain_priority",
        "description": "Why an item has its priority: score, band, reason and the rubric factors.",
        "input_schema": {
            "type": "object",
            "properties": {"item_ref": _ITEM_REF},
            "required": ["item_ref"],
        },
    },
    {
        "name": "delegate_item",
        "description": (
            "Propose handing an item to someone in the CEO's contacts. Returns a read-back "
            "to say, or a question if the name is ambiguous or unknown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_ref": _ITEM_REF,
                "delegate_name": {"type": "string", "description": "The name as the CEO said it."},
                "due": {**_DATE, "description": "Deadline, YYYY-MM-DD, if the CEO gave one."},
                "handoff_type": {"type": "string", "enum": ["full_ownership", "keep_me_updated"]},
                "note": {"type": "string", "description": "Extra instructions from the CEO."},
            },
            "required": ["item_ref", "delegate_name"],
        },
    },
    {
        "name": "decline_meeting",
        "description": "Propose declining a meeting, with an optional note to the organizer.",
        "input_schema": {
            "type": "object",
            "properties": {"item_ref": _ITEM_REF, "note": {"type": "string"}},
            "required": ["item_ref"],
        },
    },
    {
        "name": "defer_item",
        "description": "Propose moving an item to a later date.",
        "input_schema": {
            "type": "object",
            "properties": {"item_ref": _ITEM_REF, "until": _DATE},
            "required": ["item_ref", "until"],
        },
    },
    {
        "name": "close_task",
        "description": "Propose marking a task as done (also at its source).",
        "input_schema": {
            "type": "object",
            "properties": {"item_ref": _ITEM_REF},
            "required": ["item_ref"],
        },
    },
    {
        "name": "drop_item",
        "description": "Propose dropping an item from the CEO's list.",
        "input_schema": {
            "type": "object",
            "properties": {"item_ref": _ITEM_REF},
            "required": ["item_ref"],
        },
    },
    {
        "name": "set_priority",
        "description": (
            "The CEO says an item belongs in another band. Applies it and learns from it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"item_ref": _ITEM_REF, "band": {"type": "string", "enum": _BANDS}},
            "required": ["item_ref", "band"],
        },
    },
    {
        "name": "set_preference",
        "description": (
            "Save a standing preference, e.g. 'Always accept meetings from Meera' or "
            "'Never accept meetings before 10 am'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"rule": {"type": "string"}},
            "required": ["rule"],
        },
    },
    {
        "name": "add_contact",
        "description": (
            "Propose adding a person to the CEO's team, so they can be delegated to and "
            "emailed. Use the name and email address exactly as the CEO said them. Returns a "
            "read-back; nothing changes until the CEO says yes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "email": {"type": "string"},
                "areas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What they look after, e.g. finance, hiring.",
                },
            },
            "required": ["full_name", "email"],
        },
    },
    {
        "name": "remove_contact",
        "description": (
            "Propose removing a person from the CEO's team. Returns a read-back; nothing "
            "changes until the CEO says yes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"who": {"type": "string", "description": "Their name."}},
            "required": ["who"],
        },
    },
    {
        "name": "search_email",
        "description": (
            "Search the CEO's mailboxes (Outlook and Gmail). Use words, a person's name or an "
            "address. Returns the newest matches with their message_id and source."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "reply_email",
        "description": (
            "Propose replying to an email found with search_email. Write the reply text the "
            "CEO asked for. Returns a read-back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "source": {"type": "string", "enum": ["outlook", "gmail"]},
                "text": {"type": "string", "description": "The reply, as the CEO wants it."},
            },
            "required": ["message_id", "source", "text"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Propose a new email from the CEO to someone in their contacts, or to an address "
            "the CEO said themselves. Returns a read-back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "A name as the CEO said it, or an address.",
                },
                "subject": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["to", "subject", "text"],
        },
    },
    {
        "name": "read_teams",
        "description": (
            "Read the CEO's Microsoft Teams chats: the latest chats, or the recent messages "
            "with one person or group when 'person' is given."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"person": {"type": "string"}},
        },
    },
    {
        "name": "send_teams_message",
        "description": (
            "Propose sending a Teams chat message to a person (contact or someone the CEO "
            "already chats with) or to a chat_id from read_teams. Returns a read-back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "A name, an address or a chat_id."},
                "text": {"type": "string"},
            },
            "required": ["to", "text"],
        },
    },
    {
        "name": "schedule_meeting",
        "description": (
            "Propose booking a meeting on the CEO's calendar and inviting people from their "
            "contacts (or addresses the CEO said). Returns a read-back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date": _DATE,
                "start_time": {"type": "string", "description": "24-hour time, HH:MM."},
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 480},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "online": {"type": "boolean", "description": "Add a Teams or Meet link."},
            },
            "required": ["title", "date", "start_time"],
        },
    },
    {
        "name": "confirm_pending_action",
        "description": (
            "Execute a pending action the CEO has just approved. action_id may be 'all'. "
            "Only use when the CEO's latest message says yes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
        },
    },
    {
        "name": "cancel_pending_action",
        "description": "Cancel a pending action. action_id may be 'all'.",
        "input_schema": {
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
        },
    },
]


# --- the agent ---------------------------------------------------------------------------


class ConversationAgent:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LLMClient,
        actions: ActionService,
        briefer: Briefer,
        delegation: DelegationService,
        learning: LearningService,
        policy: ApprovalPolicy,
        rubric: Rubric,
        tz: tzinfo,
        clock: Clock,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        name: str = "Atlas",
        skills: Skills | None = None,
    ) -> None:
        self.name = name
        self.skills = skills or Skills()
        self._sessions = session_factory
        self._llm = llm
        self._actions = actions
        self._briefer = briefer
        self._delegation = delegation
        self._learning = learning
        self._policy = policy
        self._rubric = rubric
        self._tz = tz
        self._clock = clock
        self._new_id = new_id

    # -- state ------------------------------------------------------------------

    def _load(self, conversation_id: str | None) -> _State:
        cid = (conversation_id or "").strip()[:64] or self._new_id()
        with self._sessions() as s:
            row = s.get(ConversationRow, cid)
            if row is None:
                return _State(cid, [], [], [])
            return _State(cid, list(row.history), list(row.last_listed), list(row.awaiting))

    def _save(self, state: _State) -> None:
        now = self._clock()
        with self._sessions.begin() as s:
            row = s.get(ConversationRow, state.id)
            if row is None:
                row = ConversationRow(id=state.id, created_at=now)
                s.add(row)
            row.history = state.history[-MAX_HISTORY:]
            row.last_listed = state.last_listed
            row.awaiting = state.awaiting
            row.updated_at = now

    def _still_pending(self, ids: list[str]) -> list[str]:
        return [a.id for a in self._actions.pending(ids)]

    # -- entry point ------------------------------------------------------------

    def converse(
        self, text: str, conversation_id: str | None = None, via: ApprovedVia = ApprovedVia.VOICE
    ) -> ConverseResponse:
        state = self._load(conversation_id)
        state.awaiting = self._still_pending(state.awaiting)
        turn = _Turn(text=spoken_addresses(text.strip()[:2000]), via=via)
        if not turn.text:
            reply = "Sorry, I didn't catch that."
        elif self._policy.forbidden_request(turn.text):
            with self._sessions.begin() as s:
                audit(s, "request.refused", "refused", actor="ceo", text=turn.text[:200])
            reply = self._policy.refusal
        else:
            fast = self._dialogue_rules(state, turn)
            reply = fast if fast is not None else self._ask_model(state, turn)

        state.awaiting = self._still_pending(state.awaiting)
        state.history += [
            {"role": "user", "text": turn.text},
            {"role": "assistant", "text": reply},
        ]
        self._save(state)
        pending = self._actions.pending(state.awaiting) if state.awaiting else []
        return ConverseResponse(
            conversation_id=state.id,
            reply=reply,
            pending_actions=pending,
            executed=turn.executed,
            needs_confirmation=bool(pending),
            brief=turn.brief,
            needs_account=turn.needs_account,
        )

    # -- dialogue rules -----------------------------------------------------------

    def _dialogue_rules(self, state: _State, turn: _Turn) -> str | None:
        text = turn.text
        words = len(text.split())
        if words <= 8 and _BRIEF.search(text) and not _SKIP.search(text):
            return self._brief(state, turn, evening=bool(_EVENING.search(text)))
        if _ALL.search(text) and words <= 10:
            return self._go_ahead_all(state, turn)
        if is_affirmation(text):
            if state.awaiting:
                return self._execute(state, turn, state.awaiting)
            pending = [a.id for a in self._actions.pending()]
            if len(pending) == 1:
                return self._execute(state, turn, pending)
            if pending:
                return self._summary(state, pending)
            return None
        if _CANCEL.match(text) and state.awaiting:
            cancelled = [self._actions.cancel(i) for i in state.awaiting]
            state.awaiting = []
            if len(cancelled) == 1:
                return "Okay, I won't do that."
            return f"Okay, I've cancelled all {len(cancelled)}."
        if _SKIP.search(text) and words <= 8:
            targets = state.awaiting or [a.id for a in self._actions.pending()]
            match = _ORDINAL.search(text)
            if targets and match:
                return self._skip(state, targets, match.group(1).lower())
        return None

    def _execute(self, state: _State, turn: _Turn, ids: list[str]) -> str:
        results = self._actions.confirm_many(ids, turn.via)
        turn.executed += results
        state.awaiting = [i for i in state.awaiting if i not in ids]
        return " ".join(r.message for r in results) or "There was nothing left to do."

    def _summary(self, state: _State, ids: list[str]) -> str:
        actions = self._actions.pending(ids)
        state.awaiting = [a.id for a in actions]
        if len(actions) == 1:
            return actions[0].readback
        parts = [f"{n}: {_strip_confirm(a.readback)}." for n, a in enumerate(actions, 1)]
        return f"I have {len(actions)} things ready. " + " ".join(parts) + " Go ahead with all?"

    def _go_ahead_all(self, state: _State, turn: _Turn) -> str:
        pending = [a.id for a in self._actions.pending()]
        if not pending:
            return "There's nothing waiting for your go-ahead."
        # The full set was already read back (in a brief or a summary): run it.
        if set(state.awaiting) == set(pending):
            return self._execute(state, turn, pending)
        return self._summary(state, pending)

    def _skip(self, state: _State, targets: list[str], word: str) -> str:
        n = _ORDINALS.get(word, int(word) if word.isdigit() else 0)
        index = len(targets) - 1 if n == -1 else n - 1
        if not 0 <= index < len(targets):
            return f"There are only {len(targets)} pending."
        action = self._actions.cancel(targets[index])
        remaining = [t for t in targets if t != action.id]
        state.awaiting = remaining
        title = clean_title(action.title)
        if remaining:
            return f"Okay, skipping {title}. Shall I go ahead with the rest?"
        return f"Okay, skipping {title}."

    # -- brief ---------------------------------------------------------------------

    def _brief(self, state: _State, turn: _Turn, *, evening: bool) -> str:
        brief = self._briefer.evening() if evening else self._briefer.morning()
        turn.brief = brief
        state.last_listed = [i.id for i in brief.items]
        if evening:
            return brief.text
        staged = [a for a in (self._stage(i, state.id) for i in brief.items) if a is not None]
        state.awaiting = [a.id for a in staged]
        return brief.text

    def _stage(self, item: BriefItem, conversation_id: str) -> PendingAction | None:
        """Turn a brief suggestion into a pending action; the brief was its read-back."""
        try:
            if item.suggested == "delegate" and item.delegate_id:
                with self._sessions() as s:
                    row = s.get(ItemRow, item.id)
                    if row is None:
                        return None
                    proposal = self._delegate_proposal(s, row, item.delegate_id, None, "", None)
                if isinstance(proposal, str):
                    return None
                return self._actions.propose(
                    ActionKind.DELEGATE, item.id, proposal[0], proposal[1], conversation_id
                )
            if item.suggested == "decline" and item.kind == ItemKind.MEETING:
                return self._actions.propose(
                    ActionKind.DECLINE_MEETING,
                    item.id,
                    self._decline_readback(item.title, item.start),
                    {},
                    conversation_id,
                )
            if item.suggested in ("decline", "drop"):
                return self._actions.propose(
                    ActionKind.DROP,
                    item.id,
                    f"Dropping {clean_title(item.title)} from your list. Confirm?",
                    {},
                    conversation_id,
                )
        except ActionError:
            log.warning("could not stage %s for %s", item.suggested, item.id)
        return None

    def _decline_readback(self, title: str, start: datetime | None) -> str:
        when = f" at {spoken_time(start, self._tz)}" if start else ""
        return f"Declining {clean_title(title)}{when} with a polite note to the organizer. Confirm?"

    # -- model ---------------------------------------------------------------------

    def _context(self, state: _State) -> str:
        now = self._clock().astimezone(self._tz)
        today = now.date()
        with self._sessions() as s:
            rows = list(
                s.scalars(
                    select(ItemRow)
                    .where(ItemRow.status == ItemStatus.OPEN)
                    .order_by(ItemRow.priority_score.desc().nulls_last(), ItemRow.start)
                    .limit(MAX_AGENDA)
                )
            )
            listed = {i: n for n, i in enumerate(state.last_listed, 1)}
            # Keep the numbering the CEO heard; add anything else after it.
            ordered = sorted(rows, key=lambda r: listed.get(r.id, len(listed) + 1))
            state.last_listed = [r.id for r in ordered]
            names = display_names(all_delegates(s))
            lines = [self._agenda_line(n, r, names, today) for n, r in enumerate(ordered, 1)]
        pending = self._actions.pending()
        pending_lines = [f"{a.id}: {_strip_confirm(a.readback)}" for a in pending]
        return (
            f"Now: {now.strftime('%A %d %B %Y, %H:%M')} ({self._tz})\n"
            f"Agenda:\n{untrusted('agenda', chr(10).join(lines) or 'Nothing open.')}\n"
            f"Pending actions:\n" + ("\n".join(pending_lines) or "None")
        )

    def _agenda_line(self, n: int, r: ItemRow, names: dict[str, str], today: date) -> str:
        parts = [f"#{n}", f"id={r.id}", r.kind.value, json.dumps(r.title)]
        if r.start:
            local = r.start.astimezone(self._tz)
            parts.append(f"at {spoken_day(local.date(), today)} {spoken_time(r.start, self._tz)}")
        if r.due:
            parts.append(f"due {spoken_day(r.due.astimezone(self._tz).date(), today)}")
        if r.priority_band:
            parts.append(r.priority_band.value)
        if r.suggested_delegate_id:
            parts.append(f"suggest {names.get(r.suggested_delegate_id, r.suggested_delegate_id)}")
        return " | ".join(parts)

    def _system(self) -> list[str]:
        with self._sessions() as s:
            delegates = all_delegates(s)
            prefs = preferences_block(s)
        team = [
            {"name": d.full_name, "aliases": d.aliases, "owns": d.ownership_areas}
            for d in delegates
        ]
        blocks = [
            load_prompt("conversation").text,
            f"Your name is {self.name}. The CEO may start a request with your name; "
            "that is just how they get your attention.",
            "The CEO's team:\n" + json.dumps(team, sort_keys=True),
            "CEO preferences:\n" + ("\n".join(f"- {p}" for p in prefs) or "- none yet"),
        ]
        facts = self.skills.memory.all() if self.skills.memory else []
        if facts:
            blocks.append("Things the CEO asked you to remember:\n" + "\n".join(facts))
        style = self.skills.thinking.style() if self.skills.thinking else ""
        if style:
            blocks.append("How the CEO writes (use it for any message you draft):\n" + style)
        return blocks

    def _ask_model(self, state: _State, turn: _Turn) -> str:
        # Plain dicts; the SDK validates the shape when the request is sent.
        messages: list[Any] = []
        for h in state.history[-MAX_HISTORY:]:
            role = "user" if h.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": str(h.get("text", ""))})
        messages.append(
            {"role": "user", "content": f"{self._context(state)}\n\nThe CEO said: {turn.text}"}
        )
        system = self._system()
        texts: list[str] = []
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = self._llm.call(
                    Task.CONVERSATION,
                    "conversation",
                    system=system,
                    messages=messages,
                    max_tokens=1500,
                    tools=[*TOOLS, *SKILL_TOOLS],
                )
                texts = [b.text for b in response.content if b.type == "text"]
                uses = [b for b in response.content if b.type == "tool_use"]
                if not uses:
                    break
                messages.append({"role": "assistant", "content": _assistant_blocks(response)})
                results = []
                for use in uses:
                    output = self._run_tool(state, turn, use.name, dict(use.input or {}))
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": use.id,
                            "content": json.dumps(output, default=str),
                        }
                    )
                messages.append({"role": "user", "content": results})
            else:
                texts = texts or ["Sorry, that took too many steps. Could you say it more simply?"]
        except BudgetExceededError:
            return (
                "I've reached this month's AI budget, so I can only handle simple commands: "
                "brief me, yes, cancel, or skip one."
            )
        except LLMError as exc:
            log.warning("conversation call failed: %s", exc)
            return "Sorry, I couldn't work that out just now. Could you try again?"

        reply = spoken_reply(" ".join(texts))
        if turn.questions and not reply:
            reply = turn.questions[0]
        reply = _with_readbacks(reply, [a.readback for a in self._actions.pending(turn.created)])
        if not reply and turn.executed:
            reply = " ".join(r.message for r in turn.executed)
        return reply or "Okay."

    # -- tools -----------------------------------------------------------------------

    def _resolve(self, s: Session, ref: str, state: _State) -> ItemRow | str:
        ref = (ref or "").strip()
        if not ref:
            return "Which item do you mean?"
        row = s.get(ItemRow, ref)
        if row is not None:
            return row
        number = re.fullmatch(r"#?\s*(\d{1,3})", ref)
        if number:
            n = int(number.group(1))
            if 1 <= n <= len(state.last_listed):
                found = s.get(ItemRow, state.last_listed[n - 1])
                if found is not None:
                    return found
            return f"There's no item number {n}."
        open_rows = list(s.scalars(select(ItemRow).where(ItemRow.status == ItemStatus.OPEN)))
        at = _TIME.search(ref)
        if at:
            hour = int(at.group(1)) % 12 + (12 if at.group(3).lower() == "pm" else 0)
            minute = int(at.group(2) or 0)
            today = self._clock().astimezone(self._tz).date()
            timed = [
                r
                for r in open_rows
                if r.kind == ItemKind.MEETING
                and r.start is not None
                and r.start.astimezone(self._tz).date() in (today, today + timedelta(days=1))
                and r.start.astimezone(self._tz).time() == time(hour, minute)
            ]
            if len(timed) == 1:
                return timed[0]
        scored = sorted(
            ((r, fuzz.token_set_ratio(ref.lower(), r.title.lower())) for r in open_rows),
            key=lambda pair: pair[1],
            reverse=True,
        )
        if scored and scored[0][1] >= 70:
            if len(scored) > 1 and scored[1][1] == scored[0][1]:
                return f"Do you mean {scored[0][0].title} or {scored[1][0].title}?"
            return scored[0][0]
        return f"I couldn't find {ref} on your list."

    def _run_tool(self, state: _State, turn: _Turn, name: str, args: dict[str, Any]) -> Any:
        try:
            handler = getattr(self, f"_tool_{name}")
        except AttributeError:
            return {"error": f"unknown tool {name}"}
        try:
            result: Any = handler(state, turn, args)
        except AuthExpiredError as exc:
            account = "google" if exc.service == "google" else "outlook"
            if name in ("read_teams", "send_teams_message"):
                account = "teams"
            return self._needs(turn, [account], "The sign-in for that account has expired.")
        except IntegrationError as exc:
            log.warning("tool %s failed: %s", name, exc)
            return {"error": f"That service didn't answer: {str(exc)[:200]}"}
        except (ActionError, ValueError, KeyError) as exc:
            return {"error": str(exc)}
        return result

    def _with_item(
        self, state: _State, args: dict[str, Any], fn: Callable[[Session, ItemRow], Any]
    ) -> Any:
        with self._sessions() as s:
            found = self._resolve(s, str(args.get("item_ref", "")), state)
            if isinstance(found, str):
                return {"error": found}
            return fn(s, found)

    def _propose(
        self,
        state: _State,
        turn: _Turn,
        kind: ActionKind,
        item_id: str | None,
        readback: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        action = self._actions.propose(kind, item_id, readback, params, state.id)
        turn.created.append(action.id)
        state.awaiting = [*[a for a in state.awaiting if a != action.id], action.id]
        return {"status": "pending", "action_id": action.id, "readback": readback}

    def _tool_list_items(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        with self._sessions() as s:
            stmt = select(ItemRow).where(ItemRow.status == ItemStatus.OPEN)
            if args.get("band"):
                stmt = stmt.where(ItemRow.priority_band == PriorityBand(args["band"]))
            if args.get("kind"):
                stmt = stmt.where(ItemRow.kind == ItemKind(args["kind"]))
            rows = list(
                s.scalars(stmt.order_by(ItemRow.priority_score.desc().nulls_last(), ItemRow.start))
            )
            if args.get("date"):
                day = date.fromisoformat(str(args["date"]))
                rows = [r for r in rows if self._on_day(r, day)]
            rows = rows[:MAX_AGENDA]
            state.last_listed = [r.id for r in rows]
            names = display_names(all_delegates(s))
            today = self._clock().astimezone(self._tz).date()
            return {
                "items": untrusted(
                    "items",
                    "\n".join(self._agenda_line(n, r, names, today) for n, r in enumerate(rows, 1))
                    or "none",
                )
            }

    def _on_day(self, r: ItemRow, day: date) -> bool:
        when = r.start or r.due
        if when is None:
            return r.kind == ItemKind.EMAIL_ACTION
        return when.astimezone(self._tz).date() == day

    def _tool_get_item(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        def detail(_s: Session, r: ItemRow) -> Any:
            return {
                "id": r.id,
                "kind": r.kind.value,
                "band": r.priority_band.value if r.priority_band else None,
                "score": r.priority_score,
                "reason": r.reason,
                "start": r.start.astimezone(self._tz).isoformat() if r.start else None,
                "due": r.due.astimezone(self._tz).isoformat() if r.due else None,
                "people": len(r.participants or []),
                "content": untrusted(
                    r.source.value, f"Title: {r.title}\nFrom: {r.requester}\n{r.description[:1500]}"
                ),
            }

        return self._with_item(state, args, detail)

    def _tool_explain_priority(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        def explain(s: Session, r: ItemRow) -> Any:
            pre = prescore(r, self._rubric, all_delegates(s), self._clock())
            return {
                "title": untrusted(r.source.value, r.title),
                "band": r.priority_band.value if r.priority_band else "not scored yet",
                "score": r.priority_score,
                "reason": r.reason,
                "rubric_factors": pre.criteria,
                "requester_tier": pre.requester_tier,
                "hard_rule": pre.hard_rule.name if pre.hard_rule else None,
            }

        return self._with_item(state, args, explain)

    def _delegate_proposal(
        self,
        s: Session,
        item: ItemRow,
        delegate_id: str,
        due_text: str | None,
        note: str,
        handoff: str | None,
    ) -> tuple[str, dict[str, Any]] | str:
        delegates = all_delegates(s)
        delegate = next((d for d in delegates if d.id == delegate_id), None)
        if delegate is None:
            return "I don't have that person in your contacts."
        _adapters, notifiers = self._delegation.notifiers()
        channels = reachable_channels(delegate, notifiers)
        name = display_names(delegates).get(delegate.id, delegate.full_name)
        if not channels:
            return f"I have no way to reach {name} yet. Add an email or chat id to their contact."
        due = date.fromisoformat(due_text) if due_text else None
        if due is None and item.due is not None:
            due = item.due.astimezone(self._tz).date()
        today = self._clock().astimezone(self._tz).date()
        readback = delegation_readback(name, item, due, channels, self._tz, today)
        params = {
            "delegate_id": delegate.id,
            "due": due.isoformat() if due else None,
            "handoff_type": handoff or "full_ownership",
            "note": note,
        }
        return readback, params

    def _tool_delegate_item(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        def propose(s: Session, item: ItemRow) -> Any:
            delegates = all_delegates(s)
            context = f"{item.title} {item.description[:500]}"
            match = NameResolver(delegates).resolve(str(args.get("delegate_name", "")), context)
            if match.delegate is None:
                question = match.question or "Who should I give it to?"
                turn.questions.append(question)
                return {"status": "question", "question": question}
            proposal = self._delegate_proposal(
                s,
                item,
                match.delegate.id,
                args.get("due") or None,
                str(args.get("note") or ""),
                args.get("handoff_type"),
            )
            if isinstance(proposal, str):
                turn.questions.append(proposal)
                return {"status": "question", "question": proposal}
            readback, params = proposal
            return self._propose(state, turn, ActionKind.DELEGATE, item.id, readback, params)

        return self._with_item(state, args, propose)

    def _tool_decline_meeting(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        def propose(_s: Session, item: ItemRow) -> Any:
            if item.kind != ItemKind.MEETING:
                return {"error": "That item is not a meeting."}
            note = str(args.get("note") or "")
            readback = self._decline_readback(item.title, item.start)
            return self._propose(
                state, turn, ActionKind.DECLINE_MEETING, item.id, readback, {"note": note}
            )

        return self._with_item(state, args, propose)

    def _tool_defer_item(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        until = date.fromisoformat(str(args.get("until", "")))
        today = self._clock().astimezone(self._tz).date()
        if until <= today:
            return {"error": "The new date must be in the future."}

        def propose(_s: Session, item: ItemRow) -> Any:
            readback = f"Moving {clean_title(item.title)} to {spoken_day(until, today)}. Confirm?"
            return self._propose(
                state, turn, ActionKind.DEFER, item.id, readback, {"until": until.isoformat()}
            )

        return self._with_item(state, args, propose)

    def _tool_close_task(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        def propose(_s: Session, item: ItemRow) -> Any:
            where = ""
            if item.kind == ItemKind.TASK:
                where = f" in {item.source.value.capitalize()}"
            readback = f"Marking {clean_title(item.title)} as done{where}. Confirm?"
            return self._propose(state, turn, ActionKind.CLOSE_TASK, item.id, readback, {})

        return self._with_item(state, args, propose)

    def _tool_drop_item(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        def propose(_s: Session, item: ItemRow) -> Any:
            readback = f"Dropping {clean_title(item.title)} from your list. Confirm?"
            return self._propose(state, turn, ActionKind.DROP, item.id, readback, {})

        return self._with_item(state, args, propose)

    # -- email, Teams and meetings ------------------------------------------------------

    def _needs(self, turn: _Turn, accounts: list[str], why: str) -> dict[str, Any]:
        """Ask for an account: the app shows a Connect button for each one."""
        turn.needs_account += [a for a in accounts if a not in turn.needs_account]
        names = {"outlook": "Outlook", "google": "Google", "teams": "Microsoft Teams"}
        spoken = " or ".join(names[a] for a in accounts)
        message = f"{why} Connect {spoken} in the Apps tab, and ask me again."
        return {"status": "needs_account", "message": message}

    def _said_by_ceo(self, state: _State, turn: _Turn, address: str) -> bool:
        said = [turn.text] + [
            str(h.get("text", "")) for h in state.history if h.get("role") == "user"
        ]
        return any(address.lower() in s.lower() for s in said)

    def _person(self, state: _State, turn: _Turn, who: str) -> tuple[str, str] | str:
        """(name, address) for someone the CEO named, or a question to ask."""
        who = who.strip()
        if _ADDRESS.fullmatch(who) and self._said_by_ceo(state, turn, who):
            with self._sessions() as s:
                known = next(
                    (d for d in all_delegates(s) if str(d.email).lower() == who.lower()), None
                )
            return (known.full_name if known else who), who.lower()
        with self._sessions() as s:
            match = NameResolver(all_delegates(s)).resolve(who)
        if match.delegate is None:
            return match.question or "Who do you mean?"
        return match.delegate.full_name, str(match.delegate.email).lower()

    def _tool_add_contact(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        name = " ".join(str(args.get("full_name", "")).split())
        address = spoken_addresses(str(args.get("email", "")).strip()).lower()
        if not name:
            return self._ask(turn, "What's their name?")
        if not _ADDRESS.fullmatch(address):
            return self._ask(turn, f"What's {name}'s email address?")
        # Only an address the CEO said themselves, never one read out of an email.
        if not self._said_by_ceo(state, turn, address):
            return self._ask(turn, f"What's {name}'s email address? Please say it to me.")
        areas = [a.strip() for a in args.get("areas") or [] if str(a).strip()][:10]
        with self._sessions() as s:
            team = all_delegates(s)
        same = next((d for d in team if str(d.email).lower() == address), None)
        if same is not None:
            return {"error": f"{same.full_name} already has that address on your team."}
        person = Delegate(
            id=new_contact_id(name, {d.id for d in team}),
            full_name=name,
            aliases=[name.split()[0]],
            email=address,
            ownership_areas=areas,
        )
        looks_after = f", who looks after {_and_list(areas)}" if areas else ""
        readback = f"Adding {name}, {address}{looks_after}, to your team. Confirm?"
        params = {
            "title": f"Add {name} to your team",
            "delegate": person.model_dump(mode="json"),
        }
        return self._propose(state, turn, ActionKind.ADD_CONTACT, None, readback, params)

    def _tool_remove_contact(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        who = str(args.get("who", "")).strip()
        with self._sessions() as s:
            match = NameResolver(all_delegates(s)).resolve(who)
        if match.delegate is None:
            return self._ask(turn, match.question or "Who should I remove?")
        person = match.delegate
        readback = f"Removing {person.full_name}, {person.email}, from your team. Confirm?"
        params = {"title": f"Remove {person.full_name} from your team", "delegate_id": person.id}
        return self._propose(state, turn, ActionKind.REMOVE_CONTACT, None, readback, params)

    def _ask(self, turn: _Turn, question: str) -> dict[str, Any]:
        turn.questions.append(question)
        return {"status": "question", "question": question}

    def _tool_search_email(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        mailboxes = self._delegation.adapters().mail
        if not mailboxes:
            return self._needs(turn, ["outlook", "google"], "Your email isn't connected yet.")
        query = str(args.get("query", "")).strip()[:200]
        if not query:
            return {"error": "What should I search for?"}
        found = [m for box in mailboxes for m in box.search(query, limit=8)]
        found.sort(key=lambda m: m.received_at.timestamp() if m.received_at else 0, reverse=True)
        today = self._clock().astimezone(self._tz).date()
        lines = []
        for m in found[:8]:
            when = ""
            if m.received_at:
                local = m.received_at.astimezone(self._tz)
                when = f"{spoken_day(local.date(), today)} {spoken_time(m.received_at, self._tz)}"
            lines.append(
                f"message_id={m.id} | source={m.source.value} | from {m.sender} | {when} | "
                f"{json.dumps(m.subject)} | {m.snippet[:300]}"
            )
        return {"emails": untrusted("email", "\n".join(lines) or "no matches")}

    def _tool_reply_email(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        source = Source(str(args.get("source", "")))
        box = next((m for m in self._delegation.adapters().mail if m.source == source), None)
        if box is None:
            account = "google" if source == Source.GMAIL else "outlook"
            return self._needs(turn, [account], "That mailbox isn't connected.")
        original = box.get(str(args.get("message_id", "")))
        if not original.sender:
            return {"error": "That email has no sender to reply to."}
        text = str(args.get("text", "")).strip()
        if not text:
            return {"error": "What should the reply say?"}
        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        readback = (
            f"Replying to {original.sender} about {clean_title(original.subject)}, saying: "
            f"{text} Confirm?"
        )
        params = {
            "title": f"Reply: {original.subject}",
            "source": source.value,
            "message_id": original.id,
            "to": [original.sender],
            "to_name": original.sender,
            "subject": subject,
            "text": text,
        }
        return self._propose(state, turn, ActionKind.REPLY_EMAIL, None, readback, params)

    def _tool_send_email(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        mailboxes = self._delegation.adapters().mail
        if not mailboxes:
            return self._needs(turn, ["outlook", "google"], "Your email isn't connected yet.")
        person = self._person(state, turn, str(args.get("to", "")))
        if isinstance(person, str):
            return self._ask(turn, person)
        name, address = person
        subject = str(args.get("subject", "")).strip() or "(no subject)"
        text = str(args.get("text", "")).strip()
        if not text:
            return {"error": "What should the email say?"}
        readback = f"Emailing {name} about {subject}, saying: {text} Confirm?"
        params = {
            "title": f"Email to {name}: {subject}",
            "source": mailboxes[0].source.value,
            "to": [address],
            "to_name": name,
            "subject": subject,
            "text": text,
        }
        return self._propose(state, turn, ActionKind.SEND_EMAIL, None, readback, params)

    def _match_chat(self, chats: list[Chat], who: str) -> Chat | None:
        who = who.strip().lower()
        for c in chats:
            if c.id == who or who in c.member_emails:
                return c
        scored = sorted(
            (
                (c, max(fuzz.token_set_ratio(who, n.lower()) for n in [c.topic or "", *c.members]))
                for c in chats
                if c.members or c.topic
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )
        if scored and scored[0][1] >= 75:
            return scored[0][0]
        return None

    def _tool_read_teams(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        chat = self._delegation.adapters().chat
        if chat is None:
            return self._needs(turn, ["teams"], "Teams isn't connected yet.")
        chats = chat.recent_chats(limit=20)
        person = str(args.get("person") or "").strip()
        if not person:
            lines = []
            for c in chats[:10]:
                last = c.last_message
                said = f"{last.sender}: {last.text[:200]}" if last else "no messages"
                name = c.topic or ", ".join(c.members) or "a chat"
                lines.append(f"chat_id={c.id} | {name} | {said}")
            return {"chats": untrusted("teams", "\n".join(lines) or "no chats")}
        found = self._match_chat(chats, person)
        if found is None:
            return {"error": f"I couldn't find a Teams chat with {person}."}
        messages = chat.messages(found.id, limit=10)
        lines = [f"{m.sender}: {m.text[:300]}" for m in reversed(messages)]
        return {
            "chat_id": found.id,
            "with": ", ".join(found.members) or found.topic,
            "messages": untrusted("teams", "\n".join(lines) or "no messages"),
        }

    def _tool_send_teams_message(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        chat = self._delegation.adapters().chat
        if chat is None:
            return self._needs(turn, ["teams"], "Teams isn't connected yet.")
        text = str(args.get("text", "")).strip()
        if not text:
            return {"error": "What should the message say?"}
        who = str(args.get("to", "")).strip()
        params: dict[str, Any] = {"text": text}
        person = self._person(state, turn, who)
        if isinstance(person, str):
            # Not a contact: someone the CEO already chats with in Teams.
            found = self._match_chat(chat.recent_chats(limit=50), who)
            if found is None:
                return self._ask(turn, person)
            name = found.topic or ", ".join(found.members) or who
            params.update(chat_id=found.id, to_name=name)
        else:
            name, address = person
            params.update(email=address, to_name=name)
        params["title"] = f"Teams message to {name}"
        readback = f"Sending {name} on Teams: {text} Confirm?"
        return self._propose(state, turn, ActionKind.TEAMS_MESSAGE, None, readback, params)

    def _tool_schedule_meeting(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        calendars = self._delegation.adapters().calendars
        if not calendars:
            return self._needs(turn, ["outlook", "google"], "Your calendar isn't connected yet.")
        day = date.fromisoformat(str(args.get("date", "")))
        clock = re.fullmatch(r"(\d{1,2}):(\d{2})", str(args.get("start_time", "")).strip())
        if clock is None:
            return {"error": "Give start_time as HH:MM."}
        start = datetime.combine(day, time(int(clock.group(1)), int(clock.group(2))), self._tz)
        if start <= self._clock():
            return {"error": "That time has already passed."}
        minutes = int(args.get("duration_minutes") or 30)
        names, addresses = [], []
        for who in args.get("attendees") or []:
            person = self._person(state, turn, str(who))
            if isinstance(person, str):
                return self._ask(turn, person)
            names.append(person[0])
            addresses.append(person[1])
        title = str(args.get("title", "")).strip() or "Meeting"
        online = bool(args.get("online", True))
        calendar = calendars[0]
        today = self._clock().astimezone(self._tz).date()
        link = " with a Teams link" if calendar.source == Source.OUTLOOK else " with a Meet link"
        with_whom = f" with {_and_list(names)}" if names else ""
        readback = (
            f"Booking {title} {spoken_day(day, today)} at {spoken_time(start, self._tz)} "
            f"for {minutes} minutes{with_whom}{link if online else ''}. Confirm?"
        )
        params = {
            "title": f"Meeting: {title}",
            "subject": title,
            "source": calendar.source.value,
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=minutes)).isoformat(),
            "attendees": addresses,
            "online": online,
        }
        return self._propose(state, turn, ActionKind.BOOK_MEETING, None, readback, params)

    def _tool_set_priority(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        band = PriorityBand(str(args.get("band")))
        found = self._with_item(state, args, lambda _s, item: item.id)
        if isinstance(found, dict):
            return found
        return {"status": "done", "message": self._learning.set_band(str(found), band)}

    # -- chief-of-staff skills ------------------------------------------------------

    def _tool_prep_meeting(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.prep is None:
            return {"error": "Meeting prep isn't available."}
        ref = str(args.get("item_ref", "") or "").strip()
        with self._sessions() as s:
            if ref:
                found = self._resolve(s, ref, state)
                if isinstance(found, str):
                    return {"error": found}
                item: ItemRow | None = found
            else:
                item = upcoming_meeting(s, self._clock())
            if item is None:
                return {"error": "You have no more meetings today."}
            if item.kind != ItemKind.MEETING:
                return {"error": f"{clean_title(item.title)} isn't a meeting."}
            item_id = item.id
        return {"prep": self.skills.prep.prepare(item_id)}

    def _tool_triage_inbox(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.triage is None:
            return {"error": "Inbox triage isn't available."}
        result = self.skills.triage.run()
        readbacks = []
        for d in result.drafts:
            readback = (
                f"Replying to {d.to} about {clean_title(d.subject)}, saying: {d.text} Confirm?"
            )
            params = {
                "title": f"Reply: {d.subject}",
                "source": d.source,
                "message_id": d.message_id,
                "to": [d.to],
                "to_name": d.to,
                "subject": d.subject,
                "text": d.text,
            }
            self._propose(state, turn, ActionKind.REPLY_EMAIL, None, readback, params)
            readbacks.append(readback)
        return {"summary": result.summary(), "drafted_replies": readbacks}

    def _tool_capture_meeting_notes(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.notes is None:
            return {"error": "Meeting notes aren't available."}
        notes = str(args.get("notes", "")).strip()
        if not notes:
            return self._ask(turn, "What were the notes?")
        meeting = str(args.get("meeting", "") or "").strip()
        summary = self.skills.notes.read(notes, meeting)
        tracked = self.skills.notes.track(summary, meeting)
        readbacks: list[str] = []
        unassigned: list[str] = []
        with self._sessions() as s:
            resolver = NameResolver(all_delegates(s))
            for action in tracked:
                match = resolver.resolve(action.owner) if action.owner else None
                item = s.get(ItemRow, action.item_id)
                if match is None or match.delegate is None or item is None:
                    unassigned.append(action.task)
                    continue
                proposal = self._delegate_proposal(
                    s,
                    item,
                    match.delegate.id,
                    action.due.isoformat() if action.due else None,
                    f"Agreed in {meeting or 'our meeting'}.",
                    "full_ownership",
                )
                if isinstance(proposal, str):
                    unassigned.append(action.task)
                    continue
                readback, params = proposal
                self._propose(state, turn, ActionKind.DELEGATE, item.id, readback, params)
                readbacks.append(readback)
        return {
            "summary": summary.summary,
            "decisions": summary.decisions,
            "delegations_to_confirm": readbacks,
            "on_your_list_with_no_owner": unassigned,
        }

    def _when(self, args: dict[str, Any], key: str = "start_time") -> datetime:
        day = date.fromisoformat(str(args.get("date", "")))
        raw = str(args.get(key) or "09:00")
        return datetime.combine(day, time.fromisoformat(raw), tzinfo=self._tz)

    def _tool_block_focus_time(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        workday = self.skills.workday
        minutes = int(args.get("duration_minutes") or (workday.focus_minutes if workday else 90))
        day = date.fromisoformat(str(args.get("date", "")))
        if args.get("start_time"):
            start = self._when(args)
        else:
            if workday is None:
                return {"error": "Say a start time."}
            with self._sessions() as s:
                found = first_free_slot(s, day, self._clock(), self._tz, workday, minutes)
            if found is None:
                return {"error": f"There's no free {minutes} minutes that day. Pick a time."}
            start = found
        end = start + timedelta(minutes=minutes)
        today = self._clock().astimezone(self._tz).date()
        readback = (
            f"Holding {spoken_time(start, self._tz)} to {spoken_time(end, self._tz)} "
            f"{spoken_day(day, today)} as focus time. Confirm?"
        )
        params = {
            "title": "Focus time",
            "subject": "Focus time",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "attendees": [],
            "online": False,
        }
        return self._propose(state, turn, ActionKind.BOOK_MEETING, None, readback, params)

    def _tool_move_meeting(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        start = self._when(args)
        today = self._clock().astimezone(self._tz).date()

        def propose(_s: Session, item: ItemRow) -> Any:
            if item.kind != ItemKind.MEETING:
                return {"error": "That item is not a meeting."}
            length = (item.end - item.start) if item.start and item.end else timedelta(hours=1)
            end = start + length
            readback = (
                f"Moving {clean_title(item.title)} to {spoken_day(start.date(), today)} at "
                f"{spoken_time(start, self._tz)}; the attendees get the update. Confirm?"
            )
            params = {"start": start.isoformat(), "end": end.isoformat()}
            return self._propose(state, turn, ActionKind.MOVE_MEETING, item.id, readback, params)

        return self._with_item(state, args, propose)

    def _tool_remind_me(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.reminders is None:
            return {"error": "Reminders aren't available."}
        text = " ".join(str(args.get("text", "")).split())
        if not text:
            return self._ask(turn, "What should I remind you of?")
        at = self._when(args, "time")
        if at <= self._clock():
            return {"error": "That time has passed. When should I remind you?"}
        self.skills.reminders.add(text, at)
        today = self._clock().astimezone(self._tz).date()
        return {
            "status": "saved",
            "when": f"{spoken_day(at.date(), today)} at {spoken_time(at, self._tz)}",
        }

    def _tool_list_reminders(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.reminders is None:
            return {"error": "Reminders aren't available."}
        far = self._clock().astimezone(self._tz).date() + timedelta(days=60)
        with self._sessions() as s:
            open_items = self.skills.reminders.due_on(s, far)
            waiting = [f"{who} has {n} emails waiting for you" for who, n in unanswered_senders(s)]
        return {"open_promises_and_reminders": open_items, "people_waiting": waiting}

    def _tool_waiting_on(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.pulse is None:
            return {"error": "That isn't available."}
        waiting = self.skills.pulse.waiting_on()
        return {"waiting_on": waiting or ["Nothing: no delegated work is open."]}

    def _tool_team_pulse(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.pulse is None:
            return {"error": "That isn't available."}
        return {"pulse": self.skills.pulse.weekly()}

    def _tool_delegation_done(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.followups is None:
            return {"error": "That isn't available."}
        followups = self.skills.followups

        def finish(_s: Session, item: ItemRow) -> Any:
            return item.id

        found = self._with_item(state, args, finish)
        if isinstance(found, dict):
            return found
        title = followups.finish(str(found))
        return {"status": "closed", "title": title}

    def _tool_remember(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.memory is None:
            return {"error": "Memory isn't available."}
        fact = str(args.get("fact", "")).strip()
        if not fact:
            return self._ask(turn, "What should I remember?")
        return {"status": "remembered", "fact": self.skills.memory.remember(fact)}

    def _tool_forget(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.memory is None:
            return {"error": "Memory isn't available."}
        return {"forgotten": self.skills.memory.forget(str(args.get("fact", "")))}

    def _tool_recall(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.memory is None:
            return {"error": "Memory isn't available."}
        found = self.skills.memory.recall([str(args.get("about", ""))])
        return {"facts": found or ["Nothing remembered about that."]}

    def _tool_think_through(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.thinking is None:
            return {"error": "That isn't available."}
        question = str(args.get("question", "")).strip() or turn.text
        return {"answer": self.skills.thinking.think_through(question)}

    def _tool_write_draft(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.thinking is None:
            return {"error": "That isn't available."}
        draft = self.skills.thinking.write(
            str(args.get("kind", "") or "note"), str(args.get("brief", "") or turn.text)
        )
        return {"draft": draft}

    def _tool_reminder_done(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        if self.skills.reminders is None:
            return {"error": "Reminders aren't available."}
        closed = self.skills.reminders.done(str(args.get("text", "")))
        return {"closed": closed}

    def _tool_set_preference(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        pref = self._learning.add_rule(str(args.get("rule", "")))
        return {"status": "saved", "rule": pref.rule_text}

    def _targets(self, state: _State, action_id: str) -> list[str]:
        if action_id.strip().lower() == "all":
            return [a.id for a in self._actions.pending()]
        return [action_id] if self._actions.pending([action_id]) else []

    def _tool_confirm_pending_action(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        # Defence in depth: only the CEO's own words can approve, whatever the model thinks.
        if not _YES_WORDS.search(turn.text):
            return {"error": "The CEO has not said yes in this message. Ask them to confirm."}
        ids = self._targets(state, str(args.get("action_id", "")))
        if not ids:
            return {"error": "No such pending action."}
        if any(i in turn.created for i in ids):
            return {"error": "Read the action back and wait for the CEO's answer first."}
        results = self._actions.confirm_many(ids, turn.via)
        turn.executed += results
        state.awaiting = [i for i in state.awaiting if i not in ids]
        return {"results": [r.model_dump() for r in results]}

    def _tool_cancel_pending_action(self, state: _State, turn: _Turn, args: dict[str, Any]) -> Any:
        ids = self._targets(state, str(args.get("action_id", "")))
        if not ids:
            return {"error": "No such pending action."}
        for i in ids:
            self._actions.cancel(i)
        state.awaiting = [i for i in state.awaiting if i not in ids]
        return {"status": "cancelled", "count": len(ids)}


def _assistant_blocks(response: Message) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for b in response.content:
        if b.type == "text":
            blocks.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return blocks


__all__ = ["TOOLS", "ConversationAgent", "ConverseResponse", "is_affirmation", "spoken_reply"]
