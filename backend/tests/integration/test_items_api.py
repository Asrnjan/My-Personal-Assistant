from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db.models import ItemRow
from app.domain.enums import PriorityBand, Source
from app.integrations.base import CalendarEvent, EmailMessage, ExternalTask
from app.integrations.fake import FakeCalendar, FakeMail, FakeTasks
from app.integrations.registry import Adapters
from app.llm.fake import FakeSender, echo_scores
from app.main import create_app
from tests.conftest import API_SECRET

NOW = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)  # 09:00 IST, Friday 16 Oct


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    start = NOW + timedelta(hours=7)
    adapters = Adapters(
        calendars=[
            FakeCalendar(
                events={
                    "m1": CalendarEvent(
                        Source.GCAL, "m1", "Budget sync", start, start + timedelta(minutes=30)
                    ),
                    "m2": CalendarEvent(
                        Source.GCAL, "m2", "Offsite", start + timedelta(days=1), None
                    ),
                }
            )
        ],
        tasks=[
            FakeTasks(
                tasks={
                    "OPS-1": ExternalTask(
                        Source.JIRA, "OPS-1", "Contract", due=NOW + timedelta(days=1)
                    )
                }
            )
        ],
        mail=[FakeMail(inbox=[EmailMessage(Source.GMAIL, "e1", "t1", "Board deck", "b@x.com")])],
        not_configured=["asana: set ASANA_ACCESS_TOKEN"],
    )
    app = create_app(
        settings,
        adapters_factory=lambda: adapters,
        clock=lambda: NOW,
        llm_sender=FakeSender(default=echo_scores()),
    )
    with TestClient(app) as c:
        token = c.post("/auth/token", json={"secret": API_SECRET, "client_name": "t"}).json()
        c.headers["Authorization"] = f"Bearer {token['access_token']}"
        yield c
    app.state.engine.dispose()


def test_sync_then_list(api: TestClient) -> None:
    report = api.post("/sync").json()
    assert report["created"] == 4
    assert report["scored"] == 4 and report["rules_only"] is False
    assert report["not_configured"] == ["asana: set ASANA_ACCESS_TOKEN"]
    titles = {i["title"] for i in api.get("/items").json()}
    assert titles == {"Budget sync", "Offsite", "Contract", "Board deck"}

    today = api.get("/items", params={"date": "2026-10-16"}).json()
    assert {i["title"] for i in today} == {"Budget sync", "Board deck"}
    tomorrow = api.get("/items", params={"date": "2026-10-17"}).json()
    assert {i["title"] for i in tomorrow} == {"Offsite", "Contract", "Board deck"}


def test_band_filter_ordering_and_get(api: TestClient) -> None:
    api.post("/sync")
    with api.app.state.session_factory.begin() as s:  # type: ignore[attr-defined]
        for row in s.scalars(select(ItemRow)):
            if row.title in ("Budget sync", "Contract"):
                row.priority_band = PriorityBand.P1_DO_MYSELF
                row.priority_score = 90 if row.title == "Contract" else 70
    p1 = api.get("/items", params={"band": "P1_do_myself"}).json()
    assert [i["title"] for i in p1] == ["Contract", "Budget sync"]
    one = api.get(f"/items/{p1[0]['id']}")
    assert one.status_code == 200 and one.json()["priority_score"] == 90
    assert api.get("/items/nope").status_code == 404
    assert api.get("/items", params={"status": "done"}).json() == []


def test_requires_auth(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as c:
        assert c.post("/sync").status_code == 401
        assert c.get("/items").status_code == 401
    app.state.engine.dispose()
