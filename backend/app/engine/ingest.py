"""Pull meetings, tasks and actionable emails from every enabled adapter and
store them as unified ``Item`` rows.

- Meetings: today plus ``ingest.days_ahead`` days, synced incrementally with
  each provider's sync token / delta link.
- Tasks: a full snapshot each run; tasks that disappear or are done at the
  source are marked done here.
- Emails: flagged/starred/important messages since the last run.

The same meeting seen in two calendars (same iCalUID), or as an invitation
email, is stored once. The CEO's own decisions (delegated, deferred, ...) are
never overwritten by a sync; only content and terminal source states are.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock, utc_now
from app.config import AppConfig
from app.db.models import ItemRow
from app.domain.enums import ItemKind, ItemStatus, Source
from app.integrations.base import (
    CalendarAdapter,
    CalendarEvent,
    EmailAdapter,
    EmailMessage,
    ExternalTask,
    TaskAdapter,
)
from app.integrations.http import IntegrationError
from app.integrations.registry import Adapters, SyncStateStore
from app.llm.usage import start_of_day

log = logging.getLogger(__name__)

EVENTS_FEED = "events"
EMAIL_FEED = "email"

_INVITE_PREFIX = re.compile(
    r"^(updated invitation|invitation|accepted|declined|tentatively accepted|"
    r"tentative|canceled event|cancelled event|meeting request)\s*(with note)?\s*:\s*",
    re.IGNORECASE,
)
_INVITE_SUFFIX = re.compile(r"\s+@\s+.*$")  # Google: "Title @ Wed Oct 16, 4pm - 4:30pm (IST)"


def normalize_title(subject: str) -> str:
    title = _INVITE_PREFIX.sub("", subject.strip())
    title = _INVITE_SUFFIX.sub("", title)
    return re.sub(r"\s+", " ", title).strip().lower()


def content_hash(*parts: object) -> str:
    raw = json.dumps(parts, default=str, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class IngestReport:
    created: int = 0
    updated: int = 0
    closed: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    not_configured: list[str] = field(default_factory=list)


class Ingestor:
    def __init__(
        self,
        adapters: Adapters,
        session_factory: sessionmaker[Session],
        sync_state: SyncStateStore,
        config: AppConfig,
        clock: Clock = utc_now,
    ) -> None:
        self._adapters = adapters
        self._sessions = session_factory
        self._sync = sync_state
        self._config = config
        self._clock = clock

    def window(self) -> tuple[datetime, datetime]:
        start = start_of_day(self._clock(), self._config.tz)
        return start, start + timedelta(days=self._config.ingest.days_ahead + 1)

    def run(self) -> IngestReport:
        report = IngestReport(not_configured=list(self._adapters.not_configured))
        for cal in self._adapters.calendars:
            self._guard(report, cal.source, partial(self._sync_calendar, cal, report))
        for tasks in self._adapters.tasks:
            self._guard(report, tasks.source, partial(self._sync_tasks, tasks, report))
        if self._config.ingest.include_email:
            for mail in self._adapters.mail:
                self._guard(report, mail.source, partial(self._sync_email, mail, report))
        log.info(
            "ingest: %d created, %d updated, %d closed, %d duplicates, %d errors",
            report.created,
            report.updated,
            report.closed,
            report.duplicates,
            len(report.errors),
        )
        return report

    @staticmethod
    def _guard(report: IngestReport, source: Source, fn: Callable[[], None]) -> None:
        try:
            fn()
        except IntegrationError as exc:
            log.warning("ingest from %s failed: %s", source.value, exc)
            report.errors.append(f"{source.value}: {exc}")

    # -- calendars -----------------------------------------------------------

    def _sync_calendar(self, adapter: CalendarAdapter, report: IngestReport) -> None:
        start, end = self.window()
        source = adapter.source
        result = adapter.sync_events(start, end, self._sync.get(source.value, EVENTS_FEED))
        now = self._clock()
        seen: set[str] = set()
        with self._sessions.begin() as s:
            for event in result.records:
                seen.add(event.id)
                existing = _get(s, source, event.id)
                in_window = event.start is not None and start <= event.start < end
                if existing is None and (event.cancelled or not in_window):
                    continue
                self._upsert_meeting(s, event, existing, now, report)
            if result.full:
                # Anything we hold for this window that the provider no longer lists is gone.
                stale = s.scalars(
                    select(ItemRow).where(
                        ItemRow.source == source,
                        ItemRow.kind == ItemKind.MEETING,
                        ItemRow.status == ItemStatus.OPEN,
                        ItemRow.start >= start,
                        ItemRow.start < end,
                    )
                ).all()
                for row in stale:
                    if row.source_id not in seen:
                        row.status = ItemStatus.DROPPED
                        report.closed += 1
        self._sync.set(source.value, EVENTS_FEED, result.cursor)

    def _upsert_meeting(
        self,
        s: Session,
        e: CalendarEvent,
        row: ItemRow | None,
        now: datetime,
        report: IngestReport,
    ) -> None:
        dedup_key = f"ical:{e.ical_uid.lower()}" if e.ical_uid else None
        if row is None and dedup_key:
            twin = s.scalar(
                select(ItemRow).where(ItemRow.dedup_key == dedup_key, ItemRow.source != e.source)
            )
            if twin is not None:
                report.duplicates += 1
                return
        me = next((a for a in e.attendees if a.is_self), None)
        participants = sorted({a.email.lower() for a in e.attendees if a.email})
        if row is None:
            row = ItemRow(source=e.source, source_id=e.id, kind=ItemKind.MEETING, title=e.title)
            s.add(row)
            report.created += 1
        else:
            report.updated += 1
        if e.cancelled:
            _close(row, ItemStatus.DROPPED)
            row.last_synced_at = now
            return
        _set_content(
            row,
            title=e.title,
            description=e.description,
            url=e.url,
            requester=e.organizer.lower() if e.organizer else None,
            participants=participants,
            start=e.start,
            end=e.end,
            due=None,
            estimated_minutes=(
                int((e.end - e.start).total_seconds() // 60) if e.start and e.end else None
            ),
        )
        row.dedup_key = dedup_key
        if me is not None and me.response == "declined":
            _close(row, ItemStatus.DECLINED)
        row.last_synced_at = now

    # -- tasks ---------------------------------------------------------------

    def _sync_tasks(self, adapter: TaskAdapter, report: IngestReport) -> None:
        tasks = adapter.list_open()
        now = self._clock()
        source = adapter.source
        with self._sessions.begin() as s:
            seen = set()
            for t in tasks:
                seen.add(t.id)
                self._upsert_task(s, t, _get(s, source, t.id), now, report)
            open_rows = s.scalars(
                select(ItemRow).where(
                    ItemRow.source == source,
                    ItemRow.kind == ItemKind.TASK,
                    ItemRow.status.in_([ItemStatus.OPEN, ItemStatus.DEFERRED]),
                )
            ).all()
            for row in open_rows:
                if row.source_id not in seen:
                    # No longer open at the source: it was finished or reassigned there.
                    row.status = ItemStatus.DONE
                    row.last_synced_at = now
                    report.closed += 1

    def _upsert_task(
        self,
        s: Session,
        t: ExternalTask,
        row: ItemRow | None,
        now: datetime,
        report: IngestReport,
    ) -> None:
        if row is None:
            if t.done:
                return
            row = ItemRow(source=t.source, source_id=t.id, kind=ItemKind.TASK, title=t.title)
            s.add(row)
            report.created += 1
        else:
            report.updated += 1
        _set_content(
            row,
            title=t.title,
            description=t.description,
            url=t.url,
            requester=t.reporter.lower() if t.reporter else None,
            participants=sorted({p.lower() for p in (t.assignee, t.reporter) if p}),
            start=None,
            end=None,
            due=t.due,
            estimated_minutes=row.estimated_minutes,
        )
        if t.done:
            _close(row, ItemStatus.DONE)
        row.last_synced_at = now

    # -- email ---------------------------------------------------------------

    def _sync_email(self, adapter: EmailAdapter, report: IngestReport) -> None:
        now = self._clock()
        source = adapter.source
        cursor = self._sync.get(source.value, EMAIL_FEED)
        since = (
            datetime.fromisoformat(cursor)
            if cursor
            else now - timedelta(hours=self._config.ingest.email_lookback_hours)
        )
        messages = adapter.list_actionable(since)
        with self._sessions.begin() as s:
            for m in messages:
                self._upsert_email(s, m, now, report)
        self._sync.set(source.value, EMAIL_FEED, now.isoformat())

    def _upsert_email(
        self, s: Session, m: EmailMessage, now: datetime, report: IngestReport
    ) -> None:
        # Gmail and Outlook ids never collide with calendar ids, but Outlook
        # mail and calendar share Source.OUTLOOK, so prefix the id.
        source_id = f"mail:{m.id}"
        row = _get(s, m.source, source_id)
        if row is None and self._is_meeting_invite(s, m):
            report.duplicates += 1
            return
        if row is None:
            row = ItemRow(
                source=m.source, source_id=source_id, kind=ItemKind.EMAIL_ACTION, title=m.subject
            )
            s.add(row)
            report.created += 1
        else:
            report.updated += 1
        _set_content(
            row,
            title=m.subject,
            description=m.snippet,
            url=m.url,
            requester=m.sender,
            participants=sorted(
                {*(r.lower() for r in m.recipients), *([m.sender] if m.sender else [])}
            ),
            start=None,
            end=None,
            due=None,
            estimated_minutes=row.estimated_minutes,
        )
        row.dedup_key = f"thread:{m.source.value}:{m.thread_id}" if m.thread_id else None
        row.last_synced_at = now

    def _is_meeting_invite(self, s: Session, m: EmailMessage) -> bool:
        """An email about a meeting we already hold (same title, sent by a participant)."""
        title = normalize_title(m.subject)
        if not title:
            return False
        meetings = s.scalars(
            select(ItemRow).where(ItemRow.kind == ItemKind.MEETING, ItemRow.start.is_not(None))
        ).all()
        for meeting in meetings:
            if normalize_title(meeting.title) != title:
                continue
            people = {*(meeting.participants or []), meeting.requester}
            if m.sender is None or m.sender in people:
                return True
        return False


def _get(s: Session, source: Source, source_id: str) -> ItemRow | None:
    return s.scalar(select(ItemRow).where(ItemRow.source == source, ItemRow.source_id == source_id))


def _close(row: ItemRow, status: ItemStatus) -> None:
    """Apply a terminal state reported by the source, unless already closed."""
    if row.status in (ItemStatus.OPEN, ItemStatus.DEFERRED, ItemStatus.DELEGATED, None):
        row.status = status


def _set_content(
    row: ItemRow,
    *,
    title: str,
    description: str,
    url: str | None,
    requester: str | None,
    participants: list[str],
    start: datetime | None,
    end: datetime | None,
    due: datetime | None,
    estimated_minutes: int | None,
) -> None:
    digest = content_hash(title, description, requester, participants, start, end, due)
    if row.content_hash == digest:
        return
    row.title = title[:500]
    row.description = description
    row.url = url
    row.requester = requester
    row.participants = participants
    row.start = start
    row.end = end
    row.due = due
    row.estimated_minutes = estimated_minutes
    if row.content_hash is not None:
        # Content changed since it was scored; the priority engine re-scores it.
        row.scored_at = None
    row.content_hash = digest


__all__ = ["IngestReport", "Ingestor", "normalize_title"]
