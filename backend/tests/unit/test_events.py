from __future__ import annotations

import pytest

from app.events import Event, EventBus


def test_publish_reaches_subscribers_and_survives_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    seen: list[str] = []

    def boom(_e: Event) -> None:
        raise RuntimeError("handler broke")

    bus.subscribe(boom)
    unsubscribe = bus.subscribe(lambda e: seen.append(e.kind))
    assert bus.publish(Event("brief", {"text": "hi"})) == 1
    assert seen == ["brief"]
    assert "event handler failed for brief" in caplog.text

    unsubscribe()
    unsubscribe()  # idempotent
    assert bus.publish(Event("alert", {})) == 0
    assert seen == ["brief"]
