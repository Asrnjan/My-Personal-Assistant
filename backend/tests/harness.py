"""A whole backend wired to in-memory adapters, fake notifiers and a fake Claude."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from anthropic.types import Message
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import AuditLogRow, ItemRow
from app.domain.enums import Channel, Source
from app.integrations.fake import FakeCalendar, FakeMail, FakeNotifier, FakeTasks
from app.integrations.notify.base import Notifier
from app.integrations.notify.email import EmailNotifier
from app.integrations.registry import Adapters
from app.llm.fake import FakeSender, fake_message, offline_responder
from app.main import create_app
from tests.conftest import API_SECRET
from tests.unit.llm.conftest import IST, MutableClock

# Friday 16 October 2026, 09:00 in Asia/Kolkata.
NOW = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)
SONNET = "claude-sonnet-5-5"


def at_ist(hour: int, minute: int = 0, days: int = 0) -> datetime:
    return datetime(2026, 10, 16, hour, minute, tzinfo=IST) + timedelta(days=days)


def tool_call(name: str, **tool_input: Any) -> Message:
    return fake_message(tool_uses=[(name, tool_input)], stop_reason="tool_use", model=SONNET)


def say(text: str) -> Message:
    return fake_message(text, model=SONNET)


@dataclass
class Harness:
    app: FastAPI
    client: TestClient
    sender: FakeSender
    clock: MutableClock
    calendar: FakeCalendar
    tasks: FakeTasks
    mail: FakeMail
    telegram: FakeNotifier
    slack: FakeNotifier
    teams: FakeNotifier
    notifiers_enabled: list[Channel] = field(
        default_factory=lambda: [Channel.EMAIL, Channel.TELEGRAM, Channel.SLACK, Channel.TEAMS]
    )

    @property
    def sessions(self) -> sessionmaker[Session]:
        factory: sessionmaker[Session] = self.app.state.session_factory
        return factory

    def converse(self, text: str, conversation_id: str = "test") -> dict[str, Any]:
        resp = self.client.post(
            "/converse", json={"text": text, "conversation_id": conversation_id}
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body

    def sync(self) -> dict[str, Any]:
        resp = self.client.post("/sync")
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body

    def item(self, title: str) -> ItemRow:
        with self.sessions() as s:
            row = s.scalars(select(ItemRow).where(ItemRow.title == title)).one()
            s.expunge(row)
            return row

    def update_item(self, title: str, **values: Any) -> None:
        with self.sessions.begin() as s:
            row = s.scalars(select(ItemRow).where(ItemRow.title == title)).one()
            for key, value in values.items():
                setattr(row, key, value)

    def audit(self, action: str | None = None) -> list[AuditLogRow]:
        with self.sessions() as s:
            stmt = select(AuditLogRow).order_by(AuditLogRow.id)
            if action is not None:
                stmt = stmt.where(AuditLogRow.action == action)
            rows = list(s.scalars(stmt))
            for r in rows:
                s.expunge(r)
            return rows


def build_harness(settings: Settings, **responder: Any) -> Iterator[Harness]:
    clock = MutableClock(NOW)
    calendar = FakeCalendar(source=Source.GCAL)
    tasks = FakeTasks(source=Source.JIRA)
    mail = FakeMail(source=Source.GMAIL)
    telegram = FakeNotifier(channel=Channel.TELEGRAM)
    slack = FakeNotifier(channel=Channel.SLACK, reach_ceo=False)
    teams = FakeNotifier(channel=Channel.TEAMS, reach_ceo=False)
    adapters = Adapters(calendars=[calendar], mail=[mail], tasks=[tasks])
    sender = FakeSender(default=offline_responder(**responder))
    holder: dict[str, Harness] = {}

    def notifiers(built: Adapters) -> list[Notifier]:
        enabled = holder["h"].notifiers_enabled
        all_: list[Notifier] = [
            EmailNotifier(built.mail[0], "ceo@example.com"),
            telegram,
            slack,
            teams,
        ]
        return [n for n in all_ if n.channel in enabled]

    app = create_app(
        settings,
        llm_sender=sender,
        adapters_factory=lambda: adapters,
        notifiers_factory=notifiers,
        clock=clock,
    )
    with TestClient(app) as client:
        token = client.post("/auth/token", json={"secret": API_SECRET, "client_name": "t"}).json()
        client.headers["Authorization"] = f"Bearer {token['access_token']}"
        harness = Harness(app, client, sender, clock, calendar, tasks, mail, telegram, slack, teams)
        holder["h"] = harness
        yield harness
    app.state.engine.dispose()


@pytest.fixture
def harness(settings: Settings) -> Iterator[Harness]:
    yield from build_harness(settings)
