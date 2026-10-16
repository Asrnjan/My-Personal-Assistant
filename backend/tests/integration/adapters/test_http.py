from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
import respx

from app.integrations.http import AuthExpiredError, HttpClient, IntegrationError, NotFoundError

BASE = "https://api.example.com"


def test_retries_rate_limit_honouring_retry_after(
    mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]
) -> None:
    sleeps: list[float] = []
    route = mock_http.get(f"{BASE}/x").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    http = make_http("svc", BASE, sleep=sleeps.append)
    assert http.get_json("/x") == {"ok": True}
    assert route.call_count == 3
    assert sleeps[0] == 3.0
    assert 0.5 <= sleeps[1] <= 0.5 * 2 * 1.25


def test_retry_after_http_date(
    mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]
) -> None:
    sleeps: list[float] = []
    mock_http.get(f"{BASE}/x").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            httpx.Response(429, headers={"Retry-After": "garbage"}),
            httpx.Response(200, json={}),
        ]
    )
    make_http("svc", BASE, sleep=sleeps.append).get_json("/x")
    assert sleeps[0] == 0.0  # a date in the past means retry now
    assert sleeps[1] > 0  # unparseable falls back to exponential backoff


def test_gives_up_after_max_retries(
    mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]
) -> None:
    route = mock_http.get(f"{BASE}/x").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(IntegrationError, match="HTTP 500") as exc:
        make_http("svc", BASE, max_retries=2).get_json("/x")
    assert exc.value.status == 500
    assert route.call_count == 3


def test_network_errors_retried_then_raised(
    mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]
) -> None:
    mock_http.get(f"{BASE}/x").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(200, json=[1])]
    )
    assert make_http("svc", BASE).get_json("/x") == [1]
    mock_http.get(f"{BASE}/y").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(IntegrationError, match="network error"):
        make_http("svc", BASE, max_retries=1).get_json("/y")


def test_401_refreshes_once_then_retries_with_new_token(
    mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]
) -> None:
    token = {"value": "old"}

    def refresh() -> bool:
        token["value"] = "new"
        return True

    route = mock_http.get(f"{BASE}/x").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={})]
    )
    http = make_http(
        "svc",
        BASE,
        auth_headers=lambda: {"Authorization": f"Bearer {token['value']}"},
        on_unauthorized=refresh,
    )
    http.get_json("/x", headers={"X-Extra": "1"})
    assert route.calls[0].request.headers["Authorization"] == "Bearer old"
    assert route.calls[1].request.headers["Authorization"] == "Bearer new"
    assert route.calls[1].request.headers["X-Extra"] == "1"


def test_error_mapping(mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]) -> None:
    mock_http.get(f"{BASE}/a").mock(return_value=httpx.Response(401))
    mock_http.get(f"{BASE}/b").mock(return_value=httpx.Response(404))
    mock_http.get(f"{BASE}/c").mock(return_value=httpx.Response(400, text="bad"))
    http = make_http("svc", BASE, on_unauthorized=lambda: False)
    with pytest.raises(AuthExpiredError):
        http.get_json("/a")
    with pytest.raises(NotFoundError):
        http.get_json("/b")
    with pytest.raises(IntegrationError, match="HTTP 400: bad"):
        http.get_json("/c")
    http.close()


def test_send_json_handles_empty_body(
    mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]
) -> None:
    mock_http.post(f"{BASE}/x").mock(return_value=httpx.Response(204))
    assert make_http("svc", BASE).send_json("POST", "/x", {"a": 1}) is None
