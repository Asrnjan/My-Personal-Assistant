"""Adapter interfaces and the records adapters return.

Adapters translate each platform's API into these records. The ingestion
engine (Milestone 4) turns them into unified ``Item`` rows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

from app.domain.enums import Source

T = TypeVar("T")


@dataclass(frozen=True)
class Attendee:
    email: str
    name: str | None = None
    response: str | None = None  # accepted | declined | tentative | needsAction
    is_self: bool = False


@dataclass(frozen=True)
class CalendarEvent:
    source: Source
    id: str
    title: str
    start: datetime | None
    end: datetime | None
    organizer: str | None = None
    attendees: tuple[Attendee, ...] = ()
    description: str = ""
    url: str | None = None
    all_day: bool = False
    cancelled: bool = False
    ical_uid: str | None = None


@dataclass(frozen=True)
class EmailMessage:
    source: Source
    id: str
    thread_id: str | None
    subject: str
    sender: str | None
    recipients: tuple[str, ...] = ()
    snippet: str = ""
    received_at: datetime | None = None
    url: str | None = None
    flagged: bool = False


@dataclass(frozen=True)
class ExternalTask:
    source: Source
    id: str
    title: str
    description: str = ""
    url: str | None = None
    assignee: str | None = None
    reporter: str | None = None
    due: datetime | None = None
    status: str = ""
    done: bool = False


@dataclass(frozen=True)
class OutboundEmail:
    to: tuple[str, ...]
    subject: str
    text: str
    html: str | None = None
    cc: tuple[str, ...] = ()
    # Provider message id to reply to; keeps the delegate's answer in the same thread.
    reply_to_message_id: str | None = None


@dataclass
class SyncResult(Generic[T]):
    """A page of changes plus the cursor to resume from next time.

    ``full`` is true when the result is a complete snapshot (the stored
    cursor was missing or expired), so missing records can be treated as gone.
    """

    records: list[T] = field(default_factory=list)
    cursor: str | None = None
    full: bool = True


class CalendarAdapter(ABC):
    source: Source

    @abstractmethod
    def sync_events(
        self, start: datetime, end: datetime, cursor: str | None = None
    ) -> SyncResult[CalendarEvent]:
        """Events in ``[start, end)``; incremental when ``cursor`` is given and still valid."""

    @abstractmethod
    def decline(self, event_id: str, note: str) -> None:
        """Decline the event for the CEO, with a note to the organizer."""

    @abstractmethod
    def add_attendee(self, event_id: str, email: str) -> None:
        """Invite someone else (the delegate) to the event."""


class EmailAdapter(ABC):
    source: Source

    @abstractmethod
    def list_actionable(self, since: datetime) -> list[EmailMessage]:
        """Flagged or starred messages, and messages that need a reply, received since ``since``."""

    @abstractmethod
    def list_thread_replies(self, thread_id: str, since: datetime) -> list[EmailMessage]:
        """Messages in a thread after ``since`` (used to detect acknowledgements)."""

    @abstractmethod
    def send(self, message: OutboundEmail) -> str:
        """Send from the CEO's account; returns the provider's message id."""


class TaskAdapter(ABC):
    source: Source

    @abstractmethod
    def list_open(self) -> list[ExternalTask]:
        """Open tasks assigned to or created by the CEO."""

    @abstractmethod
    def reassign(self, task_id: str, platform_user_id: str) -> None: ...

    @abstractmethod
    def comment(self, task_id: str, text: str) -> None: ...

    @abstractmethod
    def close(self, task_id: str) -> None: ...
