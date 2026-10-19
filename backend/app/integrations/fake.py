"""In-memory adapters for tests and the offline demo. They record every
outbound action so callers can assert on what would have been sent."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from app.domain.delegate import Delegate
from app.domain.enums import Channel, Source
from app.integrations.base import (
    CalendarAdapter,
    CalendarEvent,
    Chat,
    ChatAdapter,
    ChatMessage,
    EmailAdapter,
    EmailMessage,
    ExternalTask,
    NewEvent,
    OutboundEmail,
    SentEmail,
    SyncResult,
    TaskAdapter,
)
from app.integrations.http import IntegrationError
from app.integrations.notify.base import AckEvent, DelegationMessage, Delivery, Notifier


@dataclass
class FakeCalendar(CalendarAdapter):
    source: Source = Source.GCAL
    events: dict[str, CalendarEvent] = field(default_factory=dict)
    # Events returned by the next incremental sync (when a cursor is passed).
    changes: list[CalendarEvent] = field(default_factory=list)
    declined: list[tuple[str, str]] = field(default_factory=list)
    added: list[tuple[str, str]] = field(default_factory=list)
    created: list[NewEvent] = field(default_factory=list)
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

    def create_event(self, event: NewEvent) -> CalendarEvent:
        if self.fail_with:
            raise self.fail_with
        self.created.append(event)
        return CalendarEvent(
            self.source, f"new-{len(self.created)}", event.title, event.start, event.end
        )


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

    def send(self, message: OutboundEmail) -> SentEmail:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(message)
        n = len(self.sent)
        return SentEmail(f"sent-{n}", f"thread-{n}")

    def search(self, query: str, limit: int = 10) -> list[EmailMessage]:
        words = query.lower().split()
        found = [
            m
            for m in self.inbox
            if all(w in f"{m.subject} {m.sender} {m.snippet}".lower() for w in words)
        ]
        found.sort(key=lambda m: m.received_at.timestamp() if m.received_at else 0, reverse=True)
        return found[:limit]

    def get(self, message_id: str) -> EmailMessage:
        for m in self.inbox:
            if m.id == message_id:
                return m
        raise IntegrationError("fake", f"no message {message_id}", status=404)


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


@dataclass
class FakeChat(ChatAdapter):
    chats: list[Chat] = field(default_factory=list)
    history: dict[str, list[ChatMessage]] = field(default_factory=dict)
    sent: list[tuple[str, str]] = field(default_factory=list)  # (chat id or email, text)
    fail_with: IntegrationError | None = None

    def recent_chats(self, limit: int = 10) -> list[Chat]:
        if self.fail_with:
            raise self.fail_with
        return self.chats[:limit]

    def messages(self, chat_id: str, limit: int = 10) -> list[ChatMessage]:
        return self.history.get(chat_id, [])[:limit]

    def send(self, chat_id: str, text: str) -> ChatMessage:
        if self.fail_with:
            raise self.fail_with
        self.sent.append((chat_id, text))
        return ChatMessage(f"msg-{len(self.sent)}", "CEO", text)

    def send_to_person(self, email: str, text: str) -> ChatMessage:
        return self.send(email, text)


@dataclass
class FakeNotifier(Notifier):
    """Records what it would send. ``acks`` are returned once by ``poll_ack``."""

    channel: Channel = Channel.TELEGRAM
    sent: list[DelegationMessage] = field(default_factory=list)
    nudges: list[tuple[str, str]] = field(default_factory=list)
    ceo_messages: list[str] = field(default_factory=list)
    acks: list[AckEvent] = field(default_factory=list)
    reach_ceo: bool = True
    fail_with: Exception | None = None

    def can_reach(self, delegate: Delegate) -> bool:
        if self.channel == Channel.TELEGRAM:
            return bool(delegate.telegram_chat_id)
        if self.channel == Channel.SLACK:
            return bool(delegate.slack_user_id)
        if self.channel == Channel.TEAMS:
            return bool(delegate.teams_webhook)
        return bool(delegate.email)

    def send_delegation(self, message: DelegationMessage) -> Delivery:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(message)
        return Delivery(self.channel, {"message_id": f"{self.channel.value}-{len(self.sent)}"})

    def send_nudge(self, delegate: Delegate, text: str, ref: dict[str, Any]) -> None:
        if self.fail_with:
            raise self.fail_with
        self.nudges.append((delegate.id, text))

    def notify_ceo(self, text: str) -> bool:
        if not self.reach_ceo:
            return False
        self.ceo_messages.append(text)
        return True

    def poll_ack(self, ref: dict[str, Any], seen: set[str]) -> list[AckEvent]:
        found = [a for a in self.acks if a.reply_id not in seen]
        self.acks = []
        return found
