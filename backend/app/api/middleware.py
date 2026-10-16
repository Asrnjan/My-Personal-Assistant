"""HTTP hardening: per-client rate limits and security headers.

Both are plain ASGI middleware so WebSocket connections pass straight through.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Paths that are never limited: health checks are polled by tunnels and monitors.
_EXEMPT = frozenset({"/health"})
# Brute-forcing the shared secret is the one attack worth a much tighter limit.
_AUTH_PATH = "/auth/token"
_MAX_KEYS = 10_000

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "media-src 'self' data: blob:",
        # The PWA can point at a backend on another origin (Settings → backend URL).
        "connect-src 'self' https: wss: ws:",
        "worker-src 'self'",
        "manifest-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ]
)
# Swagger UI and ReDoc load their own scripts from a CDN, so they get no CSP.
_NO_CSP_PREFIXES = ("/docs", "/redoc", "/openapi.json")
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "microphone=(self), camera=(), geolocation=(), payment=()",
    "cross-origin-opener-policy": "same-origin",
}


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBuckets:
    """One token bucket per key: ``per_minute`` requests a minute, bursting to the same."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: str) -> float:
        """Spend one token. Returns 0 when allowed, else seconds until one is free."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.pop(key, None) or _Bucket(self._capacity, now)
            bucket.tokens = min(self._capacity, bucket.tokens + (now - bucket.updated) * self._rate)
            bucket.updated = now
            self._buckets[key] = bucket
            while len(self._buckets) > _MAX_KEYS:
                self._buckets.popitem(last=False)
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return 0.0
            return (1 - bucket.tokens) / self._rate


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return bytes(value).decode("latin-1")
    return None


def client_key(scope: Scope, trust_proxy: bool) -> str:
    """Bearer token (hashed) when present, otherwise the client's address."""
    auth = _header(scope, b"authorization") or ""
    if auth.lower().startswith("bearer ") and len(auth) > 7:
        return "t:" + hashlib.sha256(auth[7:].encode()).hexdigest()[:24]
    return "ip:" + client_ip(scope, trust_proxy)


def client_ip(scope: Scope, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = _header(scope, b"cf-connecting-ip") or _header(scope, b"x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        auth_per_minute: int,
        trust_proxy: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._general = TokenBuckets(per_minute, clock) if per_minute > 0 else None
        self._auth = TokenBuckets(auth_per_minute, clock) if auth_per_minute > 0 else None
        self._trust_proxy = trust_proxy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _EXEMPT:
            await self._app(scope, receive, send)
            return
        wait = 0.0
        if scope.get("path") == _AUTH_PATH and self._auth is not None:
            wait = self._auth.take("ip:" + client_ip(scope, self._trust_proxy))
        if not wait and self._general is not None:
            wait = self._general.take(client_key(scope, self._trust_proxy))
        if wait:
            await _too_many(send, wait)
            return
        await self._app(scope, receive, send)


async def _too_many(send: Send, wait: float) -> None:
    body = json.dumps({"detail": "Too many requests; slow down."}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(max(1, math.ceil(wait))).encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        with_csp = not path.startswith(_NO_CSP_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[Any] = list(message.get("headers", []))
                present = {bytes(k).lower() for k, _v in headers}
                extra = dict(SECURITY_HEADERS)
                if with_csp:
                    extra["content-security-policy"] = CSP
                for name, value in extra.items():
                    if name.encode() not in present:
                        headers.append((name.encode(), value.encode()))
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_headers)
