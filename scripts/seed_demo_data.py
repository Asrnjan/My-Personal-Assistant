"""Offline demo: a CEO's Friday morning, start to finish, with no network and no API key.

    backend/.venv/bin/python scripts/seed_demo_data.py            # throwaway database
    backend/.venv/bin/python scripts/seed_demo_data.py --db demo.db   # keep the result

It seeds a calendar, a Jira board and an inbox with in-memory adapters, stands in
for Claude with a deterministic offline responder, and then walks through:

1. a sync and the spoken morning brief,
2. "go ahead with all": delegations sent by email and Telegram, a Jira task
   reassigned, a low-value meeting declined, noise dropped,
3. Rahul replying "Accepted" by email and the follow-up closing,
4. the evening wrap-up.

Nothing leaves the machine: outbound messages are printed instead of sent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from anthropic.types import Message  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from app.config import Settings  # noqa: E402
from app.domain.enums import Channel, Source  # noqa: E402
from app.integrations.base import (  # noqa: E402
    Attendee,
    CalendarEvent,
    EmailMessage,
    ExternalTask,
)
from app.integrations.fake import FakeCalendar, FakeMail, FakeNotifier, FakeTasks  # noqa: E402
from app.integrations.notify.base import Notifier  # noqa: E402
from app.integrations.notify.email import EmailNotifier  # noqa: E402
from app.integrations.registry import Adapters  # noqa: E402
from app.llm.fake import FakeSender, fake_message, offline_responder  # noqa: E402
from app.main import create_app  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
FRIDAY_9AM = datetime(2026, 10, 16, 9, 0, tzinfo=IST)
SECRET = "demo-secret"  # noqa: S105 - local demo only

# How the stand-in "Claude" scores each demo item: band, score, reason, delegate.
VERDICTS: dict[str, tuple[str, int, str, str | None]] = {
    "Board deck for Q3 review": (
        "P1_do_myself",
        92,
        "The board meets Monday and only you can present the numbers.",
        None,
    ),
    "Vendor contract review": (
        "P2_delegate",
        74,
        "Renewal is due this month; Rahul owns vendor management.",
        "rahul",
    ),
    "Q4 hiring plan draft": (
        "P2_delegate",
        66,
        "Arjun owns hiring and can draft it for your review.",
        "arjun",
    ),
    "Investor call: Series B": (
        "P1_do_myself",
        88,
        "Fundraising: the investors expect you on the call.",
        None,
    ),
    "Weekly marketing sync": (
        "P3_decline",
        35,
        "A status meeting with no decision for you; Meera can cover it.",
        None,
    ),
    "Newsletter: 10 productivity hacks": ("P4_drop", 5, "A newsletter, no action needed.", None),
}
_ITEM = re.compile(r"Item (\{.*?\})\n<untrusted[^>]*>\nTitle: (.*)")


def demo_responder() -> Any:
    """Offline stand-in for Claude that scores the demo items realistically."""
    fallback = offline_responder()

    def respond(params: dict[str, Any]) -> Message:
        first = params["messages"][0]["content"]
        user = first if isinstance(first, str) else json.dumps(first)
        found = _ITEM.findall(user)
        if not found or params.get("tools"):
            return fallback(params)
        scores = []
        for meta, title in found:
            band, score, reason, delegate = VERDICTS.get(
                title.strip(), ("P2_delegate", 50, "Routine.", None)
            )
            scores.append(
                {
                    "item_id": json.loads(meta)["id"],
                    "score": score,
                    "band": band,
                    "reason": reason,
                    "confidence": 0.9,
                    "ceo_required": band == "P1_do_myself",
                    "suggested_delegate_id": delegate,
                }
            )
        return fake_message({"scores": scores}, model=params["model"])

    return respond


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now


class PrintingNotifier(FakeNotifier):
    """Records like the test fake and prints what would have been sent."""

    def send_delegation(self, message: Any) -> Any:
        delivery = super().send_delegation(message)
        print(f"    [{self.channel.value}] to {message.delegate.full_name}: {message.subject}")
        return delivery

    def notify_ceo(self, text: str) -> bool:
        print(f"    [{self.channel.value}] to you: {text}")
        return super().notify_ceo(text)


def at(hour: int, minute: int = 0, days: int = 0) -> datetime:
    return FRIDAY_9AM.replace(hour=hour, minute=minute) + timedelta(days=days)


def seed(calendar: FakeCalendar, tasks: FakeTasks, mail: FakeMail) -> None:
    calendar.events = {
        "evt-marketing": CalendarEvent(
            Source.GCAL,
            "evt-marketing",
            "Weekly marketing sync",
            at(16),
            at(16, 30),
            organizer="meera.shah@example.com",
            attendees=(
                Attendee("ceo@example.com", "You", "accepted", is_self=True),
                Attendee("meera.shah@example.com", "Meera Shah", "accepted"),
            ),
        ),
        "evt-investor": CalendarEvent(
            Source.GCAL, "evt-investor", "Investor call: Series B", at(11), at(11, 45)
        ),
    }
    tasks.tasks = {
        "OPS-42": ExternalTask(
            Source.JIRA,
            "OPS-42",
            "Vendor contract review",
            description="Acme's contract renews on 31 October. Check pricing and terms.",
            url="https://example.atlassian.net/browse/OPS-42",
            due=at(18),
        ),
        "HR-7": ExternalTask(Source.JIRA, "HR-7", "Q4 hiring plan draft", due=at(17)),
        "FIN-3": ExternalTask(Source.JIRA, "FIN-3", "Board deck for Q3 review", due=at(18)),
    }
    mail.inbox = [
        EmailMessage(
            Source.GMAIL,
            "msg-news",
            "thread-news",
            "Newsletter: 10 productivity hacks",
            "news@example.org",
            snippet="This week's top tips...",
            received_at=at(7),
        )
    ]


def say(client: TestClient, text: str) -> dict[str, Any]:
    print(f'\nYou: "{text}"')
    resp = client.post("/converse", json={"text": text, "conversation_id": "demo"})
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    print(f"Assistant: {body['reply']}")
    return body


def heading(text: str) -> None:
    print(f"\n=== {text} " + "=" * max(0, 60 - len(text)))


def run(db_path: Path) -> dict[str, Any]:
    url = f"sqlite:///{db_path}"
    cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")

    clock = Clock(FRIDAY_9AM)
    calendar, tasks, mail = FakeCalendar(), FakeTasks(), FakeMail()
    seed(calendar, tasks, mail)
    adapters = Adapters(calendars=[calendar], mail=[mail], tasks=[tasks])
    telegram = PrintingNotifier(channel=Channel.TELEGRAM)
    teams = PrintingNotifier(channel=Channel.TEAMS, reach_ceo=False)

    def notifiers(built: Adapters) -> list[Notifier]:
        return [EmailNotifier(built.mail[0], "ceo@example.com"), telegram, teams]

    settings = Settings(
        database_url=url,
        api_auth_token=SecretStr(SECRET),
        scheduler_enabled=False,
        telegram_polling=False,
        pwa_dir=None,
        rate_limit_per_minute=0,
    )
    app = create_app(
        settings,
        llm_sender=FakeSender(default=demo_responder()),
        adapters_factory=lambda: adapters,
        notifiers_factory=notifiers,
        clock=clock,
    )
    with TestClient(app) as client:
        token = client.post("/auth/token", json={"secret": SECRET, "client_name": "demo"})
        client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"

        heading("Friday 09:00: syncing calendar, Jira and Gmail")
        synced = client.post("/sync").json()
        print(json.dumps({k: synced[k] for k in sorted(synced) if k != "errors"}, default=str))

        heading("Morning brief")
        say(client, "brief me")
        print("\nOutbound so far: none. Everything waits for your yes.")

        heading("Approving the suggestions")
        print("Sending:")
        approved = say(client, "go ahead with all")
        for result in approved["executed"]:
            print(f"    - {result['message']}")
        print(f"\nJira reassigned: {tasks.reassigned}")
        print(f"Calendar declined: {[e for e, _note in calendar.declined]}")
        print(f"Emails sent: {[m.to for m in mail.sent]}")

        heading("Friday 14:00: Rahul replies to the email")
        clock.now += timedelta(hours=5)
        first = mail.sent[0]
        mail.threads["thread-1"] = [
            EmailMessage(
                Source.GMAIL,
                "reply-1",
                "thread-1",
                f"Re: {first.subject}",
                "rahul.kumar@example.com",
                snippet="Accepted, on it. I'll send a recommendation by Monday.",
                received_at=clock.now,
            )
        ]
        report = app.state.followups.run()
        print(
            f"Follow-up check: {report.acknowledged} acknowledged, {report.nudged} reminded "
            "(Arjun's draft is due within 24 hours)"
        )
        for fu in client.get("/followups").json():
            print(f"    - {fu['title']}: {fu['delegate_name']} ({fu['state']})")

        heading("Friday 18:30: evening wrap-up")
        clock.now = at(18, 30).astimezone(UTC)
        evening = client.get("/brief/today", params={"kind": "evening"}).json()
        print(evening["text"])

        audit = app.state.session_factory
        from sqlalchemy import func, select

        from app.db.models import AuditLogRow

        with audit() as s:
            entries = s.scalar(select(func.count()).select_from(AuditLogRow))
        print(f"\nAudit log: {entries} entries. Database: {db_path}")
        summary = {
            "delegations_sent": sum(m.subject.startswith("Delegated") for m in mail.sent),
            "emails_sent": len(mail.sent),
            "telegram_messages": len(telegram.sent),
            "jira_reassigned": len(tasks.reassigned),
            "meetings_declined": len(calendar.declined),
            "acknowledged": report.acknowledged,
            "audit_entries": entries,
        }
    app.state.engine.dispose()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, help="SQLite file to keep (default: a temp file)")
    args = parser.parse_args(argv)
    if args.db:
        args.db.unlink(missing_ok=True)
        run(args.db.resolve())
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        run(Path(tmp) / "demo.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
