from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from assistant_desktop.auth import (
    AuthError,
    BackendError,
    TokenProvider,
    TokenStore,
    exchange_secret,
)

BASE = "http://backend.test"


def token_transport(calls: list[dict[str, object]], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/token"
        body = json.loads(request.content)
        calls.append(body)
        if status != 200:
            return httpx.Response(status, json={"detail": "nope"})
        return httpx.Response(
            200,
            json={
                "access_token": f"tok-{len(calls)}",
                "token_type": "bearer",
                "client_name": "desktop",
            },
        )

    return httpx.MockTransport(handler)


def test_exchange_secret_success() -> None:
    calls: list[dict[str, object]] = []
    with httpx.Client(transport=token_transport(calls)) as http:
        assert exchange_secret(http, BASE + "/", "s", "desktop") == "tok-1"
    assert calls == [{"secret": "s", "client_name": "desktop"}]


@pytest.mark.parametrize(("status", "error"), [(401, AuthError), (503, BackendError)])
def test_exchange_secret_errors(status: int, error: type[Exception]) -> None:
    with httpx.Client(transport=token_transport([], status)) as http, pytest.raises(error):
        exchange_secret(http, BASE, "s", "desktop")


@pytest.mark.parametrize("body", [b"not json", b'{"other": 1}', b'{"access_token": ""}', b"[1]"])
def test_exchange_secret_bad_body(body: bytes) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=body))
    with httpx.Client(transport=transport) as http, pytest.raises(BackendError):
        exchange_secret(http, BASE, "s", "desktop")


def test_exchange_secret_network_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(fail)) as http,
        pytest.raises(BackendError, match="cannot reach"),
    ):
        exchange_secret(http, BASE, "s", "desktop")


def test_token_store_roundtrip_and_permissions(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "sub" / "token.json")
    assert store.load(BASE) is None
    store.save(BASE, "abc")
    assert store.load(BASE) == "abc"
    assert store.load("http://other") is None
    if os.name == "posix":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.clear()
    store.clear()  # idempotent
    assert store.load(BASE) is None


@pytest.mark.parametrize("content", ["{broken", "[]", f'{{"backend_url": "{BASE}"}}'])
def test_token_store_ignores_bad_cache(tmp_path: Path, content: str) -> None:
    path = tmp_path / "token.json"
    path.write_text(content)
    assert TokenStore(path).load(BASE) is None


def test_provider_exchanges_once_and_caches(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    store = TokenStore(tmp_path / "token.json")
    with httpx.Client(transport=token_transport(calls)) as http:
        provider = TokenProvider(http, BASE, secret="s", store=store)
        assert provider.get() == "tok-1"
        assert provider.get() == "tok-1"
        assert len(calls) == 1
        assert store.load(BASE) == "tok-1"
        # A fresh provider (next start) reuses the cache without a request.
        assert TokenProvider(http, BASE, secret="s", store=store).get() == "tok-1"
        assert len(calls) == 1
        # A rejected token is dropped and replaced.
        assert provider.invalidate() is True
        assert store.load(BASE) is None
        assert provider.get() == "tok-2"


def test_provider_prefers_configured_token() -> None:
    with httpx.Client(transport=token_transport([])) as http:
        provider = TokenProvider(http, BASE, secret="s", token="fixed")
        assert provider.get() == "fixed"
        assert provider.can_refresh is False
        assert provider.invalidate() is False
        assert provider.get() == "fixed"


def test_provider_without_secret_or_token() -> None:
    with httpx.Client(transport=token_transport([])) as http:
        provider = TokenProvider(http, BASE)
        with pytest.raises(AuthError, match="no access token"):
            provider.get()
        assert provider.invalidate() is False


def test_provider_survives_unwritable_cache(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = TokenStore(blocker / "token.json")  # parent is a file: save fails
    with httpx.Client(transport=token_transport([])) as http:
        assert TokenProvider(http, BASE, secret="s", store=store).get() == "tok-1"
