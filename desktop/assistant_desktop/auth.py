"""Access tokens: exchange the shared secret once, then reuse the cached token.

``POST /auth/token {"secret", "client_name"}`` returns ``{"access_token", ...}``.
The token is cached as JSON (mode 0600) together with the backend URL it was
issued by, so pointing the client at another backend never reuses it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """No usable access token: missing secret, or the backend rejected it."""


class BackendError(RuntimeError):
    """The backend could not be reached or answered with an error."""


class TokenStore:
    """A one-entry token cache on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, backend_url: str) -> str | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable token cache %s: %s", self.path, exc)
            return None
        if not isinstance(data, dict) or data.get("backend_url") != backend_url:
            return None
        token = data.get("access_token")
        return token if isinstance(token, str) and token else None

    def save(self, backend_url: str, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"backend_url": backend_url, "access_token": token}, fh)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def exchange_secret(http: httpx.Client, base_url: str, secret: str, client_name: str) -> str:
    """Trade the shared secret for a per-client access token."""
    try:
        response = http.post(
            base_url.rstrip("/") + "/auth/token",
            json={"secret": secret, "client_name": client_name},
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"cannot reach the backend at {base_url}: {exc}") from exc
    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise AuthError("the backend rejected the shared secret")
    if response.status_code != httpx.codes.OK:
        raise BackendError(f"token exchange failed: HTTP {response.status_code}")
    try:
        token = response.json()["access_token"]
    except (ValueError, KeyError, TypeError):
        raise BackendError("token exchange returned an unexpected body") from None
    if not isinstance(token, str) or not token:
        raise BackendError("token exchange returned an empty token")
    return token


class TokenProvider:
    """Order of preference: configured token, cached token, fresh exchange."""

    def __init__(
        self,
        http: httpx.Client,
        base_url: str,
        *,
        secret: str = "",
        token: str = "",
        client_name: str = "desktop",
        store: TokenStore | None = None,
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._secret = secret
        self._configured = token
        self._client_name = client_name
        self._store = store
        self._current: str | None = token or None
        self._lock = threading.Lock()

    @property
    def can_refresh(self) -> bool:
        return bool(self._secret) and not self._configured

    def get(self) -> str:
        with self._lock:
            if self._current:
                return self._current
            if self._store is not None:
                cached = self._store.load(self._base_url)
                if cached:
                    self._current = cached
                    return cached
            if not self._secret:
                raise AuthError(
                    "no access token: set backend.secret (or CEO_DESKTOP_SECRET) to the "
                    "backend's API_AUTH_TOKEN, or backend.token to an issued token"
                )
            token = exchange_secret(self._http, self._base_url, self._secret, self._client_name)
            if self._store is not None:
                try:
                    self._store.save(self._base_url, token)
                except OSError as exc:
                    log.warning("could not cache the access token: %s", exc)
            self._current = token
            return token

    def invalidate(self) -> bool:
        """Forget a rejected token; True when :meth:`get` can fetch a new one."""
        with self._lock:
            if not self.can_refresh:
                return False
            self._current = None
            if self._store is not None:
                self._store.clear()
            return True
