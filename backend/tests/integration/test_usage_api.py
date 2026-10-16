from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.usage import UsageLedger
from app.main import create_app
from tests.conftest import API_SECRET


def test_usage_report(settings: Settings) -> None:
    now = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
    app = create_app(settings, clock=lambda: now)
    ledger: UsageLedger = app.state.usage_ledger
    ledger.record(model="claude-haiku-5-5", purpose="score", input_tokens=1000, output_tokens=10)
    with TestClient(app) as c:
        assert c.get("/usage").status_code == 401
        token = c.post("/auth/token", json={"secret": API_SECRET, "client_name": "t"}).json()[
            "access_token"
        ]
        body = c.get("/usage", headers={"Authorization": f"Bearer {token}"}).json()
    app.state.engine.dispose()
    assert body["today"]["calls"] == 1
    assert body["month"]["input_tokens"] == 1000
    assert body["monthly_budget_usd"] == 20.0
    assert body["budget_exceeded"] is False
