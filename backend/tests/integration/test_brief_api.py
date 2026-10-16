from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppConfig, Settings
from app.db.models import BriefRow
from app.domain.enums import Source
from app.events import Event
from app.integrations.base import CalendarEvent, ExternalTask
from app.integrations.fake import FakeCalendar, FakeTasks
from app.integrations.registry import Adapters
from app.llm.fake import FakeSender, echo_scores
from app.main import create_app
from app.scheduler import AssistantJobs, build_scheduler
from tests.conftest import API_SECRET

NOW = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)  # 09:00 IST


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    start = NOW + timedelta(hours=2)
    adapters = Adapters(
        calendars=[
            FakeCalendar(
                events={
                    "m1": CalendarEvent(
                        Source.GCAL, "m1", "Board prep", start, start + timedelta(minutes=30)
                    )
                }
            )
        ],
        tasks=[
            FakeTasks(
                tasks={
                    "OPS-1": ExternalTask(
                        Source.JIRA, "OPS-1", "Contract", due=NOW + timedelta(hours=6)
                    )
                }
            )
        ],
    )
    app = create_app(
        settings,
        adapters_factory=lambda: adapters,
        clock=lambda: NOW,
        llm_sender=FakeSender(default=echo_scores(score=90, band="P1_do_myself")),
    )
    with TestClient(app) as c:
        token = c.post("/auth/token", json={"secret": API_SECRET, "client_name": "t"}).json()
        c.headers["Authorization"] = f"Bearer {token['access_token']}"
        yield c
    app.state.engine.dispose()


def test_sync_alerts_once_then_brief(api: TestClient) -> None:
    events: list[Event] = []
    api.app.state.events.subscribe(events.append)  # type: ignore[attr-defined]

    assert api.get("/brief/latest").status_code == 404
    first = api.post("/sync").json()
    assert first["alerted"] == 2
    assert [e.payload["kind"] for e in events] == ["alert"]
    alert = api.get("/brief/latest", params={"kind": "alert"}).json()
    assert alert["text"] == "Heads up. 2 new top priorities: Board prep at 11 am and Contract."
    assert api.post("/sync").json()["alerted"] == 0

    today = api.get("/brief/today").json()
    assert today["kind"] == "morning"
    assert today["text"].startswith("Good morning. You have 1 meeting and 1 task today.")
    evening = api.get("/brief/today", params={"kind": "evening", "date": "2026-10-16"}).json()
    assert evening["kind"] == "evening"
    assert api.get("/brief/today", params={"kind": "alert"}).status_code == 400

    delivered = api.post("/brief/deliver", params={"kind": "morning"}).json()
    assert delivered["text"] == today["text"]
    assert [e.payload["kind"] for e in events] == ["alert", "morning"]
    assert api.get("/brief/latest").json()["kind"] == "morning"
    assert api.post("/brief/deliver", params={"kind": "evening"}).json()["kind"] == "evening"
    assert api.post("/brief/deliver", params={"kind": "alert"}).status_code == 400
    with api.app.state.session_factory() as s:  # type: ignore[attr-defined]
        kinds = [r.kind for r in s.scalars(select(BriefRow).order_by(BriefRow.created_at))]
    assert sorted(kinds) == ["alert", "evening", "morning"]


def test_brief_routes_require_auth(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.get("/brief/today").status_code == 401
        assert c.get("/brief/latest").status_code == 401
        assert c.post("/brief/deliver", params={"kind": "morning"}).status_code == 401
    app.state.engine.dispose()


def test_build_scheduler_jobs(api: TestClient) -> None:
    jobs: AssistantJobs = api.app.state.jobs  # type: ignore[attr-defined]
    config = AppConfig.model_validate({"brief_times": {"morning": "07:45", "evening": "19:00"}})
    scheduler = build_scheduler(jobs, config)
    found = {j.id: j for j in scheduler.get_jobs()}
    assert set(found) == {"morning_brief", "evening_wrapup", "sync"}
    morning = {f.name: str(f) for f in found["morning_brief"].trigger.fields}
    assert morning["hour"] == "7" and morning["minute"] == "45"
    assert str(found["morning_brief"].trigger.timezone) == "Asia/Kolkata"
    assert found["sync"].trigger.interval == timedelta(minutes=15)

    quiet = build_scheduler(
        jobs, AppConfig.model_validate({"scheduler": {"sync_interval_minutes": 0}})
    )
    assert {j.id for j in quiet.get_jobs()} == {"morning_brief", "evening_wrapup"}
    assert AppConfig().brief_times.morning == time(8, 0)


def test_morning_delivery_suppresses_alerts(api: TestClient) -> None:
    jobs: AssistantJobs = api.app.state.jobs  # type: ignore[attr-defined]
    jobs._sync.run()  # ingest + score without the alert step
    jobs.morning_brief()
    assert api.post("/sync").json()["alerted"] == 0
    with pytest.raises(ValueError, match="alerts"):
        jobs.deliver_kind("alert")  # type: ignore[arg-type]


def test_background_sync_never_raises(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    jobs: AssistantJobs = api.app.state.jobs  # type: ignore[attr-defined]
    jobs.background_sync()
    assert api.get("/brief/latest", params={"kind": "alert"}).status_code == 200

    def broken() -> None:
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(jobs._sync, "run", broken)
    jobs.background_sync()
    assert "background sync failed" in caplog.text


def test_scheduler_starts_and_stops_with_app(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"scheduler_enabled": True}))
    with TestClient(app):
        pass
    app.state.engine.dispose()
