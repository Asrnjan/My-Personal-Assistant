from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx

from app.integrations.http import HttpClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def mock_http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def make_http() -> Callable[..., HttpClient]:
    def make(service: str, base_url: str, **kwargs: Any) -> HttpClient:
        kwargs.setdefault("sleep", lambda _s: None)
        kwargs.setdefault("auth_headers", lambda: {"Authorization": "Bearer test"})
        return HttpClient(service, base_url, **kwargs)

    return make
