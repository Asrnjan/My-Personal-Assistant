"""Shared Microsoft Graph helpers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from app.integrations.http import HttpClient

BASE_URL = "https://graph.microsoft.com/v1.0"
# Ask Graph for UTC so dateTime values need no timezone lookup.
UTC_PREFER = 'outlook.timezone="UTC"'


def iterate_pages(
    http: HttpClient, url: str, params: dict[str, str] | None = None, prefer: str = UTC_PREFER
) -> Iterator[dict[str, Any]]:
    """Yield each page, following ``@odata.nextLink`` (an absolute URL with its own query)."""
    next_url: str | None = url
    query = params
    while next_url:
        page: dict[str, Any] = http.get_json(next_url, params=query, headers={"Prefer": prefer})
        yield page
        next_url = page.get("@odata.nextLink")
        query = None
