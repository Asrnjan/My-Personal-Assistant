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


@dataclass(frozen=True)
class SentEmail:
    """Ids of a sent message; ``thread_id`` is what replies are tracked by."""

    message_id: str | None
    thread_id: str | None


@dataclass(frozen=True)
class NewEvent:
    """A meeting the CEO asked to book."""

    title: str
    start: datetime
    end: datetime
    attendees: tuple[str, ...] = ()
    description: str = ""
    online: bool = True  # add a Teams or Meet link


@dataclass(frozen=True)
class ChatMessage:
    id: str
    sender: str | None
    text: str
    sent_at: datetime | None = None


@dataclass(frozen=True)
class Chat:
    """A Teams chat: a one-on-one, a group chat or a meeting chat."""

    id: str
    topic: str | None
    members: tuple[str, ...]  # display names, without the CEO
    member_emails: tuple[str, ...] = ()
    last_message: ChatMessage | None = None


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

    @abstractmethod
    def create_event(self, event: NewEvent) -> CalendarEvent:
        """Book a meeting on the CEO's calendar and invite the attendees."""


class EmailAdapter(ABC):
    source: Source

    @abstractmethod
    def list_actionable(self, since: datetime) -> list[EmailMessage]:
        """Flagged or starred messages, and messages that need a reply, received since ``since``."""

    @abstractmethod
    def list_thread_replies(self, thread_id: str, since: datetime) -> list[EmailMessage]:
        """Messages in a thread after ``since`` (used to detect acknowledgements)."""

    @abstractmethod
    def send(self, message: OutboundEmail) -> SentEmail:
        """Send from the CEO's account; returns the provider's ids for the message."""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[EmailMessage]:
        """Messages matching ``query`` (words, a name or an address), newest first."""

    @abstractmethod
    def get(self, message_id: str) -> EmailMessage:
        """One message, so a reply can go to its sender."""


class ChatAdapter(ABC):
    """Reading and sending chat messages as the CEO (Microsoft Teams)."""

    @abstractmethod
    def recent_chats(self, limit: int = 10) -> list[Chat]:
        """The CEO's chats with the newest message first."""

    @abstractmethod
    def messages(self, chat_id: str, limit: int = 10) -> list[ChatMessage]:
        """The newest messages in a chat, newest first."""

    @abstractmethod
    def send(self, chat_id: str, text: str) -> ChatMessage:
        """Post a message in an existing chat."""

    @abstractmethod
    def send_to_person(self, email: str, text: str) -> ChatMessage:
        """Post in the one-on-one chat with ``email``, starting it if needed."""


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
