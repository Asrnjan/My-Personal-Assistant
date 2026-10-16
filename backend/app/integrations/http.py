"""HTTP client shared by all adapters: retries with exponential backoff,
rate-limit handling and consistent errors."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
Sleep = Callable[[float], None]


class IntegrationError(RuntimeError):
    def __init__(self, service: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status = status


class AuthExpiredError(IntegrationError):
    """Credentials were rejected; the user must reconnect the account."""


class NotFoundError(IntegrationError):
    pass


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


class HttpClient:
    """Thin wrapper over ``httpx.Client``.

    ``auth_headers`` is called on every attempt so a refreshed token is used
    on retry. A 401 triggers ``on_unauthorized`` once (e.g. an OAuth refresh)
    before the request is retried.
    """

    def __init__(
        self,
        service: str,
        base_url: str,
        *,
        auth_headers: Callable[[], Mapping[str, str]] = dict,
        on_unauthorized: Callable[[], bool] | None = None,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        max_backoff: float = 30.0,
        timeout: float = 30.0,
        sleep: Sleep = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.service = service
        self._auth_headers = auth_headers
        self._on_unauthorized = on_unauthorized
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            hinted = _retry_after_seconds(response)
            if hinted is not None:
                return min(hinted, self._max_backoff)
        delay: float = self._backoff_base * (2**attempt)
        return min(delay + random.uniform(0, delay / 4), self._max_backoff)  # noqa: S311

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        extra_headers: Mapping[str, str] = kwargs.pop("headers", None) or {}
        refreshed = False
        attempt = 0
        while True:
            headers = {**self._auth_headers(), **extra_headers}
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise IntegrationError(self.service, f"network error: {exc}") from exc
                self._sleep(self._backoff(attempt, None))
                attempt += 1
                continue

            if response.status_code == 401 and self._on_unauthorized and not refreshed:
                refreshed = True
                if self._on_unauthorized():
                    continue
            if response.status_code in RETRY_STATUSES and attempt < self._max_retries:
                delay = self._backoff(attempt, response)
                log.info(
                    "%s %s %s -> %d, retrying in %.1fs",
                    self.service,
                    method,
                    url,
                    response.status_code,
                    delay,
                )
                self._sleep(delay)
                attempt += 1
                continue
            return self._check(response)

    def _check(self, response: httpx.Response) -> httpx.Response:
        status = response.status_code
        if status < 400:
            return response
        detail = response.text[:300]
        if status in (401, 403):
            raise AuthExpiredError(self.service, f"unauthorized ({status}): {detail}", status)
        if status == 404:
            raise NotFoundError(self.service, f"not found: {detail}", status)
        raise IntegrationError(self.service, f"HTTP {status}: {detail}", status)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def send_json(self, method: str, url: str, body: Any = None, **kwargs: Any) -> Any:
        response = self.request(method, url, json=body, **kwargs)
        if not response.content:
            return None
        return response.json()
