from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.middleware import CSP, TokenBuckets, client_ip, client_key
from app.config import Settings
from app.main import create_app
from tests.conftest import API_SECRET


def make_client(settings: Settings, **overrides: object) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update=overrides))
    with TestClient(app) as c:
        yield c
    app.state.engine.dispose()


@pytest.fixture
def limited(settings: Settings) -> Iterator[TestClient]:
    yield from make_client(settings, rate_limit_per_minute=3, auth_rate_limit_per_minute=2)


def token(c: TestClient, name: str = "t") -> str:
    resp = c.post("/auth/token", json={"secret": API_SECRET, "client_name": name})
    assert resp.status_code == 200, resp.text
    value: str = resp.json()["access_token"]
    return value


def test_rate_limit_per_client(limited: TestClient) -> None:
    first = {"Authorization": f"Bearer {token(limited, 'a')}"}
    second = {"Authorization": f"Bearer {token(limited, 'b')}"}
    for _ in range(3):
        assert limited.get("/items", headers=first).status_code == 200
    blocked = limited.get("/items", headers=first)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked.headers["x-content-type-options"] == "nosniff"
    # Another client has its own allowance, and health checks are never limited.
    assert limited.get("/items", headers=second).status_code == 200
    for _ in range(10):
        assert limited.get("/health").status_code == 200


def test_auth_endpoint_has_a_tight_limit(limited: TestClient) -> None:
    wrong = {"secret": "guess", "client_name": "x"}
    assert limited.post("/auth/token", json=wrong).status_code == 401
    assert limited.post("/auth/token", json=wrong).status_code == 401
    assert limited.post("/auth/token", json=wrong).status_code == 429


def test_rate_limit_can_be_disabled(settings: Settings) -> None:
    for c in make_client(settings, rate_limit_per_minute=0, auth_rate_limit_per_minute=0):
        for _ in range(15):
            assert (
                c.post("/auth/token", json={"secret": "x", "client_name": "x"}).status_code == 401
            )


def test_token_bucket_refills() -> None:
    now = [0.0]
    buckets = TokenBuckets(60, clock=lambda: now[0])
    assert all(buckets.take("k") == 0 for _ in range(60))
    assert buckets.take("k") == pytest.approx(1.0)
    now[0] = 1.0
    assert buckets.take("k") == 0
    assert buckets.take("other") == 0


def test_client_keys() -> None:
    scope = {
        "type": "http",
        "client": ("10.0.0.1", 1),
        "headers": [(b"cf-connecting-ip", b"203.0.113.9"), (b"authorization", b"Bearer abc")],
    }
    assert client_key(scope, False).startswith("t:")
    assert client_ip(scope, False) == "10.0.0.1"
    assert client_ip(scope, True) == "203.0.113.9"
    assert client_key({"type": "http", "headers": []}, False) == "ip:unknown"
    forwarded = {"type": "http", "headers": [(b"x-forwarded-for", b"198.51.100.1, 10.0.0.1")]}
    assert client_ip(forwarded, True) == "198.51.100.1"


def test_security_headers(client: TestClient) -> None:
    page = client.get("/")
    assert page.headers["content-security-policy"] == CSP
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "microphone=(self)" in page.headers["permissions-policy"]
    docs = client.get("/docs")
    assert docs.status_code == 200 and "content-security-policy" not in docs.headers
    assert client.get("/health").headers["x-content-type-options"] == "nosniff"


def test_cors_is_off_unless_configured(settings: Settings, client: TestClient) -> None:
    preflight = {
        "Origin": "https://pwa.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization",
    }
    assert (
        "access-control-allow-origin" not in client.options("/converse", headers=preflight).headers
    )
    for c in make_client(settings, cors_origins="https://pwa.example.com, https://b.example.com"):
        allowed = c.options("/converse", headers=preflight)
        assert allowed.headers["access-control-allow-origin"] == "https://pwa.example.com"
        evil = c.options("/converse", headers={**preflight, "Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in evil.headers


def test_websocket_still_works_with_middleware(client: TestClient) -> None:
    value = token(client)
    with client.websocket_connect(f"/ws?token={value}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
