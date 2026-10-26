"""Scheduled work: morning brief, evening wrap-up, background sync with P1 alerts.

:class:`AssistantJobs` holds the job bodies so they can be called from the
API (manual sync, ``POST /brief/deliver`` for cron setups) and from tests
without a running scheduler. :func:`build_scheduler` wires them to
APScheduler in the configured timezone.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from app.config import AppConfig
from app.db.models import BriefRow
from app.engine.actions import ActionService
from app.engine.briefing import Brief, Briefer, BriefKind
from app.engine.followup import FollowUpReport, FollowUpService
from app.engine.notices import Notices
from app.engine.prep import MeetingPrep
from app.engine.pulse import TeamPulse
from app.engine.reminders import Reminders
from app.engine.sync import SyncOutcome, SyncService
from app.events import Event, EventBus

log = logging.getLogger(__name__)

# Acknowledgement checks, nudges and overdue marking.
FOLLOWUP_MINUTES = 30
# How often to look for a meeting that is about to start and needs a prep.
PREP_CHECK_MINUTES = 2
# Reminders due now; promises read from sent mail less often.
REMINDER_CHECK_MINUTES = 5
SENT_SCAN_HOURS = 3


@dataclass
class SyncAndAlert:
    outcome: SyncOutcome
    alert: Brief | None


class AssistantJobs:
    def __init__(
        self,
        sync: SyncService,
        briefer: Briefer,
        bus: EventBus,
        session_factory: sessionmaker[Session],
        followups: FollowUpService | None = None,
        actions: ActionService | None = None,
        *,
        prep: MeetingPrep | None = None,
        notices: Notices | None = None,
        reminders: Reminders | None = None,
        pulse: TeamPulse | None = None,
    ) -> None:
        self._sync = sync
        self._briefer = briefer
        self._bus = bus
        self._sessions = session_factory
        self._followups = followups
        self._actions = actions
        self._prep = prep
        self._notices = notices
        self._reminders = reminders
        self._pulse = pulse
        # The scheduler thread and a manual /sync must not ingest at the same time.
        self._sync_lock = threading.Lock()

    def deliver(self, brief: Brief) -> Brief:
        """Store a brief and push it to subscribers (WebSocket, notifiers)."""
        payload = brief.model_dump(mode="json")
        with self._sessions.begin() as s:
            s.add(
                BriefRow(
                    kind=brief.kind.value,
                    for_date=brief.for_date,
                    text=brief.text,
                    data=payload,
                    created_at=brief.generated_at,
                )
            )
        self._bus.publish(Event("brief", payload, brief.generated_at))
        log.info("delivered %s brief (%d words)", brief.kind.value, brief.word_count)
        return brief

    def morning_brief(self) -> Brief:
        brief = self._briefer.morning()
        # P1 items covered by the brief need no separate alert.
        self._briefer.mark_alerted([i.id for i in brief.items])
        return self.deliver(brief)

    def evening_wrapup(self) -> Brief:
        return self.deliver(self._briefer.evening())

    def deliver_kind(self, kind: BriefKind) -> Brief:
        if kind == BriefKind.MORNING:
            return self.morning_brief()
        if kind == BriefKind.EVENING:
            return self.evening_wrapup()
        raise ValueError("alerts are delivered by the sync job")

    def sync_and_alert(self) -> SyncAndAlert:
        with self._sync_lock:
            if self._actions is not None:
                self._actions.reopen_deferred()
            outcome = self._sync.run()
            alert = self._briefer.alerts()
        if alert is not None:
            self.deliver(alert)
        return SyncAndAlert(outcome, alert)

    def background_sync(self) -> None:
        """Scheduler entry point: never let an exception kill the job."""
        try:
            self.sync_and_alert()
        except Exception:
            log.exception("background sync failed")

    def check_followups(self) -> FollowUpReport | None:
        if self._followups is None:
            return None
        return self._followups.run()

    def background_followups(self) -> None:
        try:
            self.check_followups()
        except Exception:
            log.exception("follow-up check failed")

    def send_preps(self) -> list[str]:
        """Prep for each meeting about to start, sent to the CEO's phone and chat."""
        if self._prep is None or self._notices is None:
            return []
        sent = []
        for _item_id, text in self._prep.due():
            self._notices.tell(f"Before your next meeting: {text}")
            sent.append(text)
        return sent

    def background_preps(self) -> None:
        try:
            self.send_preps()
        except Exception:
            log.exception("meeting prep failed")

    def send_reminders(self) -> list[str]:
        if self._reminders is None or self._notices is None:
            return []
        texts = self._reminders.due_now()
        for text in texts:
            self._notices.tell(f"Reminder: {text}")
        return texts

    def background_reminders(self) -> None:
        try:
            self.send_reminders()
        except Exception:
            log.exception("reminders failed")

    def background_promises(self) -> None:
        try:
            if self._reminders is not None:
                self._reminders.scan_sent()
        except Exception:
            log.exception("reading promises from sent mail failed")

    def weekly(self) -> list[str]:
        """The Friday summaries: the team pulse (and, when wired, the activity report)."""
        texts = []
        if self._pulse is not None:
            texts.append(self._pulse.weekly())
        if self._notices is not None:
            for text in texts:
                self._notices.tell(text)
        return texts

    def background_weekly(self) -> None:
        try:
            self.weekly()
        except Exception:
            log.exception("weekly summary failed")


def build_scheduler(jobs: AssistantJobs, app_config: AppConfig) -> BackgroundScheduler:
    tz = app_config.tz
    scheduler = BackgroundScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 15 * 60},
    )
    for job_id, at, func in (
        ("morning_brief", app_config.brief_times.morning, jobs.morning_brief),
        ("evening_wrapup", app_config.brief_times.evening, jobs.evening_wrapup),
    ):
        scheduler.add_job(func, CronTrigger(hour=at.hour, minute=at.minute, timezone=tz), id=job_id)
    minutes = app_config.scheduler.sync_interval_minutes
    if minutes:
        scheduler.add_job(
            jobs.background_sync, IntervalTrigger(minutes=minutes, timezone=tz), id="sync"
        )
    scheduler.add_job(
        jobs.background_followups,
        IntervalTrigger(minutes=FOLLOWUP_MINUTES, timezone=tz),
        id="followups",
    )
    scheduler.add_job(
        jobs.background_reminders,
        IntervalTrigger(minutes=REMINDER_CHECK_MINUTES, timezone=tz),
        id="reminders",
    )
    scheduler.add_job(
        jobs.background_promises,
        IntervalTrigger(hours=SENT_SCAN_HOURS, timezone=tz),
        id="promises",
    )
    weekly = app_config.workday
    scheduler.add_job(
        jobs.background_weekly,
        CronTrigger(
            day_of_week=weekly.weekly_day,
            hour=weekly.weekly_time.hour,
            minute=weekly.weekly_time.minute,
            timezone=tz,
        ),
        id="weekly",
    )
    if app_config.workday.prep_minutes_before:
        scheduler.add_job(
            jobs.background_preps,
            IntervalTrigger(minutes=PREP_CHECK_MINUTES, timezone=tz),
            id="meeting_prep",
        )
    return scheduler
