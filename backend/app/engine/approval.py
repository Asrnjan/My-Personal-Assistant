"""Approval policy: which actions run on their own, need a yes, or are refused."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Phrases that hand work to someone else; a delegation is never itself a payment.
_DELEGATION = re.compile(
    r"^\s*(please\s+)?(delegate|assign|hand(\s+off)?|pass|forward|ask|get)\b", re.IGNORECASE
)


class Level(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"
    NEVER = "never"


DEFAULT_REFUSAL = (
    "I can't handle payments, purchases, invoices or deleting data. "
    "You'll need to do that one yourself."
)


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    auto: list[str] = Field(
        default_factory=lambda: [
            "close_done_at_source",
            "sync_status",
            "nudge_delegate",
            "set_preference",
        ]
    )
    confirm: list[str] = Field(
        default_factory=lambda: [
            "delegate",
            "decline_meeting",
            "forward_meeting",
            "reassign_task",
            "send_message",
            "close_task",
            "defer",
            "drop",
        ]
    )
    never: list[str] = Field(
        default_factory=lambda: ["payment", "purchase", "invoice_payment", "delete_data"]
    )
    never_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\b(pay|pays|paying|repay|remit|wire)\b",
            r"\btransfer\b.*\b(money|funds|payment|amount|salary)\b",
            r"\b(purchase|buy|buying)\b",
            r"\b(approve|settle|process|clear)\b.*\b(invoice|payment|bill|expense|reimbursement)s?\b",
            r"\brefund\b",
            r"\b(delete|erase|wipe|purge)\b",
        ]
    )
    # Actions that run without asking when everyone involved is on the CEO's team
    # (send_email, reply_email, teams_message, book_meeting, delegate). Anyone outside the
    # team still needs a yes. Also changeable by voice ("send emails to my team without
    # asking").
    auto_when_team: list[str] = Field(default_factory=list)
    refusal: str = DEFAULT_REFUSAL

    @model_validator(mode="after")
    def _check(self) -> ApprovalPolicy:
        seen: dict[str, str] = {}
        for level in ("auto", "confirm", "never"):
            for action in getattr(self, level):
                if action in seen:
                    raise ValueError(f"{action!r} is listed under both {seen[action]} and {level}")
                seen[action] = level
        for pattern in self.never_patterns:
            re.compile(pattern)
        return self

    @cached_property
    def _patterns(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.never_patterns]

    def level(self, action: str) -> Level:
        """The level for ``action``. Unknown actions need confirmation."""
        if action in self.never:
            return Level.NEVER
        if action in self.auto:
            return Level.AUTO
        return Level.CONFIRM

    def forbidden_request(self, utterance: str) -> bool:
        """True when the CEO asks the assistant itself to pay, buy or delete something."""
        if _DELEGATION.search(utterance):
            return False
        return any(p.search(utterance) for p in self._patterns)


def load_policy(path: Path) -> ApprovalPolicy:
    if not path.exists():
        return ApprovalPolicy()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return ApprovalPolicy.model_validate(raw)
