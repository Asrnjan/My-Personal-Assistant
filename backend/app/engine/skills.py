"""The chief-of-staff skills the conversation can use beyond the item list: meeting prep
and inbox triage. Each service is optional so the agent works without it (tests, offline).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.engine.prep import MeetingPrep
from app.engine.triage import InboxTriage


@dataclass
class Skills:
    prep: MeetingPrep | None = None
    triage: InboxTriage | None = None


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
]
