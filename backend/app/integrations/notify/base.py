"""The notifier interface every channel implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from app.domain.delegate import Delegate
from app.domain.enums import Channel


@dataclass(frozen=True)
class DelegationMessage:
    followup_id: str
    delegate: Delegate
    subject: str
    text: str
    html: str


@dataclass(frozen=True)
class Delivery:
    """Where a message went, so replies and button presses can be matched later."""

    channel: Channel
    ref: dict[str, Any] = field(default_factory=dict)


AckSignal = Literal["acknowledged", "question"]


@dataclass(frozen=True)
class AckEvent:
    signal: AckSignal
    text: str = ""
    reply_id: str | None = None


class Notifier(ABC):
    channel: Channel

    @abstractmethod
    def can_reach(self, delegate: Delegate) -> bool:
        """True when this channel has an address for ``delegate``."""

    @abstractmethod
    def send_delegation(self, message: DelegationMessage) -> Delivery:
        """Send a delegation brief, asking the delegate to accept."""

    @abstractmethod
    def send_nudge(self, delegate: Delegate, text: str, ref: dict[str, Any]) -> None:
        """Remind the delegate about an earlier delegation (``ref`` is its delivery ref)."""

    def notify_ceo(self, text: str) -> bool:
        """Tell the CEO something. Returns False when this channel can't reach the CEO."""
        return False

    def poll_ack(self, ref: dict[str, Any], seen: set[str]) -> list[AckEvent]:
        """Acknowledgements on this channel since the delivery, skipping ids in ``seen``.

        Channels where acknowledgement arrives some other way (button presses,
        email replies classified elsewhere) return nothing.
        """
        return []
