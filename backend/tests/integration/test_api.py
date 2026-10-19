from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings
from app.db.models import ApiTokenRow
from app.main import create_app
from tests.conftest import API_SECRET


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "version": "0.1.0",
        "database": "ok",
        "assistant_name": "Atlas",
    }


def test_health_degraded_when_db_unreachable(tmp_path: object) -> None:
    app = create_app(Settings(database_url="sqlite:////nonexistent-dir/x.db"))
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.json()["status"] == "degraded"
    assert resp.json()["database"] == "error"


def test_openapi_docs_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert "/health" in client.get("/openapi.json").json()["paths"]


def test_token_issue_and_use(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/auth/whoami", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"client_name": "pytest"}


def test_tokens_are_stored_hashed(
    client: TestClient, auth_headers: dict[str, str], settings: Settings
) -> None:
    raw = auth_headers["Authorization"].removeprefix("Bearer ")
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        row = s.scalars(select(ApiTokenRow)).one()
    assert row.token_hash != raw
    assert len(row.token_hash) == 64


def test_wrong_secret_rejected(client: TestClient) -> None:
    resp = client.post("/auth/token", json={"secret": "nope", "client_name": "pwa"})
    assert resp.status_code == 401


def test_bad_client_name_rejected(client: TestClient) -> None:
    resp = client.post("/auth/token", json={"secret": API_SECRET, "client_name": "a b"})
    assert resp.status_code == 422


def test_token_endpoint_disabled_without_secret(database_url: str) -> None:
    app = create_app(Settings(database_url=database_url, api_auth_token=SecretStr("")))
    with TestClient(app) as c:
        resp = c.post("/auth/token", json={"secret": "anything", "client_name": "pwa"})
    assert resp.status_code == 503


def test_missing_invalid_and_revoked_tokens(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/auth/whoami").status_code == 401
    bad = client.get("/auth/whoami", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == "Bearer"

    with client.app.state.session_factory.begin() as s:  # type: ignore[attr-defined]
        s.scalars(select(ApiTokenRow)).one().revoked_at = datetime.now(UTC)
    assert client.get("/auth/whoami", headers=auth_headers).status_code == 401
