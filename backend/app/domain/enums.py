"""Enumerations shared by the domain model, database and API."""

from __future__ import annotations

from enum import StrEnum


class Source(StrEnum):
    GCAL = "gcal"
    OUTLOOK = "outlook"
    GMAIL = "gmail"
    JIRA = "jira"
    ASANA = "asana"
    CLICKUP = "clickup"
    SLACK = "slack"


class ItemKind(StrEnum):
    TASK = "task"
    MEETING = "meeting"
    EMAIL_ACTION = "email_action"


class ItemStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    DELEGATED = "delegated"
    DECLINED = "declined"
    DEFERRED = "deferred"
    DROPPED = "dropped"


class PriorityBand(StrEnum):
    P1_DO_MYSELF = "P1_do_myself"
    P2_DELEGATE = "P2_delegate"
    P3_DECLINE = "P3_decline"
    P4_DROP = "P4_drop"


class DecisionAction(StrEnum):
    DO = "do"
    DELEGATE = "delegate"
    DECLINE = "decline"
    DEFER = "defer"
    DROP = "drop"
    CLOSE = "close"


class Actor(StrEnum):
    CEO = "ceo"
    AUTO = "auto"


class ApprovedVia(StrEnum):
    VOICE = "voice"
    TAP = "tap"
    TELEGRAM = "telegram"
    AUTO = "auto"


class Channel(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    SLACK = "slack"
    TEAMS = "teams"


class FollowUpState(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    NUDGED = "nudged"
    OVERDUE = "overdue"
    CLOSED = "closed"


class SignalKind(StrEnum):
    BAND_OVERRIDE = "band_override"
    DELEGATE_OVERRIDE = "delegate_override"
    RULE = "rule"


class ActionKind(StrEnum):
    """Actions the CEO can approve. Each maps to an approval-policy key."""

    DELEGATE = "delegate"
    DECLINE_MEETING = "decline_meeting"
    DEFER = "defer"
    CLOSE_TASK = "close_task"
    DROP = "drop"
    # Not tied to an item: things the CEO asks for in conversation.
    SEND_EMAIL = "send_email"
    REPLY_EMAIL = "reply_email"
    TEAMS_MESSAGE = "teams_message"
    BOOK_MEETING = "book_meeting"


class ActionStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"
