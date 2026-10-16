"""In-memory adapters for tests and the offline demo. They record every
outbound action so callers can assert on what would have been sent."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from app.domain.enums import Source
from app.integrations.base import (
    CalendarAdapter,
    CalendarEvent,
    EmailAdapter,
    EmailMessage,
    ExternalTask,
    OutboundEmail,
    SyncResult,
    TaskAdapter,
)
from app.integrations.http import IntegrationError


@dataclass
class FakeCalendar(CalendarAdapter):
    source: Source = Source.GCAL
    events: dict[str, CalendarEvent] = field(default_factory=dict)
    # Events returned by the next incremental sync (when a cursor is passed).
    changes: list[CalendarEvent] = field(default_factory=list)
    declined: list[tuple[str, str]] = field(default_factory=list)
    added: list[tuple[str, str]] = field(default_factory=list)
    fail_with: IntegrationError | None = None
    sync_calls: int = 0

    def sync_events(
        self, start: datetime, end: datetime, cursor: str | None = None
    ) -> SyncResult[CalendarEvent]:
        self.sync_calls += 1
        if self.fail_with:
            raise self.fail_with
        token = f"{self.source.value}-sync-{self.sync_calls}"
        if cursor:
            changes, self.changes = self.changes, []
            for e in changes:
                self.events[e.id] = e
            return SyncResult(records=changes, cursor=token, full=False)
        records = [e for e in self.events.values() if e.start is None or start <= e.start < end]
        return SyncResult(records=records, cursor=token, full=True)

    def decline(self, event_id: str, note: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.declined.append((event_id, note))

    def add_attendee(self, event_id: str, email: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.added.append((event_id, email))


@dataclass
class FakeMail(EmailAdapter):
    source: Source = Source.GMAIL
    inbox: list[EmailMessage] = field(default_factory=list)
    threads: dict[str, list[EmailMessage]] = field(default_factory=dict)
    sent: list[OutboundEmail] = field(default_factory=list)
    fail_with: IntegrationError | None = None
    since_calls: list[datetime] = field(default_factory=list)

    def list_actionable(self, since: datetime) -> list[EmailMessage]:
        self.since_calls.append(since)
        if self.fail_with:
            raise self.fail_with
        return [m for m in self.inbox if m.received_at is None or m.received_at >= since]

    def list_thread_replies(self, thread_id: str, since: datetime) -> list[EmailMessage]:
        return [
            m
            for m in self.threads.get(thread_id, [])
            if m.received_at is None or m.received_at > since
        ]

    def send(self, message: OutboundEmail) -> str:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(message)
        return f"sent-{len(self.sent)}"


@dataclass
class FakeTasks(TaskAdapter):
    source: Source = Source.JIRA
    tasks: dict[str, ExternalTask] = field(default_factory=dict)
    reassigned: list[tuple[str, str]] = field(default_factory=list)
    comments: list[tuple[str, str]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    fail_with: IntegrationError | None = None

    def list_open(self) -> list[ExternalTask]:
        if self.fail_with:
            raise self.fail_with
        return [t for t in self.tasks.values() if not t.done]

    def reassign(self, task_id: str, platform_user_id: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.reassigned.append((task_id, platform_user_id))

    def comment(self, task_id: str, text: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.comments.append((task_id, text))

    def close(self, task_id: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.closed.append(task_id)
        if task_id in self.tasks:
            self.tasks[task_id] = replace(self.tasks[task_id], done=True)
