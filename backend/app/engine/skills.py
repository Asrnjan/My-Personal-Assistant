"""The chief-of-staff skills the conversation can use beyond the item list: meeting prep,
inbox triage, meeting notes, protecting the calendar and reminders. Each service is
optional so the agent works without it (tests, offline).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import WorkdayConfig
from app.engine.followup import FollowUpService
from app.engine.notes import MeetingNotes
from app.engine.prep import MeetingPrep
from app.engine.pulse import TeamPulse
from app.engine.reminders import Reminders
from app.engine.triage import InboxTriage


@dataclass
class Skills:
    prep: MeetingPrep | None = None
    triage: InboxTriage | None = None
    notes: MeetingNotes | None = None
    reminders: Reminders | None = None
    workday: WorkdayConfig | None = None
    pulse: TeamPulse | None = None
    followups: FollowUpService | None = None


_ITEM_REF_OPTIONAL = {
    "type": "string",
    "description": "The meeting's id or agenda number; leave empty for the next meeting.",
}

SKILL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "prep_meeting",
        "description": (
            "Brief the CEO for a meeting: who is in it, what is open between them, recent "
            "emails and what to push for. Say the returned prep as it is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"item_ref": _ITEM_REF_OPTIONAL},
        },
    },
    {
        "name": "triage_inbox",
        "description": (
            "Sort the emails that need action into needs-you, can-delegate and FYI, and draft "
            "replies to the ones that need the CEO. Each draft becomes a pending reply with a "
            "read-back; say the summary, then the read-backs."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "capture_meeting_notes",
        "description": (
            "Turn notes the CEO dictated or pasted after a meeting into a summary, decisions "
            "and action items. Action items with an owner on the team become delegations "
            "with read-backs. Say the summary, then the read-backs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notes": {"type": "string", "description": "The notes, as the CEO gave them."},
                "meeting": {"type": "string", "description": "The meeting's name, if known."},
            },
            "required": ["notes"],
        },
    },
    {
        "name": "block_focus_time",
        "description": (
            "Hold time on the CEO's calendar for focused work, in the first free gap of that "
            "day unless a start time is given. Returns a read-back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD."},
                "start_time": {"type": "string", "description": "24-hour HH:MM, optional."},
                "duration_minutes": {"type": "integer", "minimum": 15, "maximum": 480},
            },
            "required": ["date"],
        },
    },
    {
        "name": "move_meeting",
        "description": (
            "Move a meeting on the CEO's calendar to another time; attendees get the update. "
            "Returns a read-back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_ref": {"type": "string", "description": "The meeting's id or number."},
                "date": {"type": "string", "description": "YYYY-MM-DD."},
                "start_time": {"type": "string", "description": "24-hour HH:MM."},
            },
            "required": ["item_ref", "date", "start_time"],
        },
    },
    {
        "name": "remind_me",
        "description": "Remind the CEO of something at a time. Done straight away, no read-back.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind them of."},
                "date": {"type": "string", "description": "YYYY-MM-DD."},
                "time": {"type": "string", "description": "24-hour HH:MM; 09:00 if not said."},
            },
            "required": ["text", "date"],
        },
    },
    {
        "name": "list_reminders",
        "description": (
            "What the CEO promised people (found in their sent emails) and asked to be "
            "reminded of, that is still open, plus people waiting on an answer."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "waiting_on",
        "description": (
            "Everything the CEO is waiting on from their team: delegated work, late items "
            "first, with deadlines and whether it was confirmed."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "team_pulse",
        "description": (
            "This week for the team: what each person finished, has open and let slip, and "
            "who may be overloaded."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "delegation_done",
        "description": "The CEO says delegated work is finished; closes its follow-up.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_ref": {"type": "string", "description": "The item's id or number."}
            },
            "required": ["item_ref"],
        },
    },
    {
        "name": "reminder_done",
        "description": "Mark a promise or reminder as done, described in the CEO's words.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]
