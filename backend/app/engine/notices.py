"""Telling the CEO about something that happened in the background."""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.domain.enums import Channel
from app.events import Event, EventBus
from app.integrations.notify.base import Notifier
from app.integrations.registry import Adapters

log = logging.getLogger(__name__)

# Push-style channels first; email is the last resort.
_ORDER = [Channel.TELEGRAM, Channel.SLACK, Channel.TEAMS, Channel.EMAIL]


class Notices:
    def __init__(
        self,
        bus: EventBus,
        adapters_factory: Callable[[], Adapters],
        notifiers_factory: Callable[[Adapters], list[Notifier]],
    ) -> None:
        self._bus = bus
        self._adapters_factory = adapters_factory
        self._notifiers_factory = notifiers_factory

    def tell(self, text: str, *, direct: bool = True) -> Channel | None:
        """Publish a notice to connected clients (WebSocket, web push) and, when
        ``direct``, also send it on the first channel that can reach the CEO.
        Returns that channel, or None."""
        self._bus.publish(Event("notice", {"text": text}))
        if not direct:
            return None
        try:
            notifiers = self._notifiers_factory(self._adapters_factory())
        except Exception:
            log.exception("could not build notifiers for a CEO notice")
            return None
        for channel in _ORDER:
            for n in notifiers:
                if n.channel != channel:
                    continue
                try:
                    if n.notify_ceo(text):
                        return channel
                except Exception:
                    log.exception("notifying the CEO via %s failed", channel.value)
        return None
