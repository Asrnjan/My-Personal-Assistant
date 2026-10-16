"""In-process publish/subscribe for push events (briefs, alerts).

The WebSocket endpoint and the notifiers subscribe here; the scheduler and
the API publish. Handlers run synchronously in the publisher's thread, so a
handler that needs an event loop must hand the event over itself.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.clock import utc_now

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    kind: str
    payload: dict[str, Any]
    at: datetime = field(default_factory=utc_now)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []
        self._lock = threading.Lock()

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        """Register ``handler``; the returned function unsubscribes it."""
        with self._lock:
            self._handlers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers:
                    self._handlers.remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> int:
        """Deliver to every handler; one failing handler does not stop the rest.

        Returns how many handlers received the event without raising.
        """
        with self._lock:
            handlers = list(self._handlers)
        delivered = 0
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                log.exception("event handler failed for %s", event.kind)
            else:
                delivered += 1
        return delivered
