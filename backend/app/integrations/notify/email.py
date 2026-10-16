"""Email notifications, sent from the CEO's own Gmail or Outlook account."""

from __future__ import annotations

from typing import Any

from app.domain.delegate import Delegate
from app.domain.enums import Channel
from app.integrations.base import EmailAdapter, OutboundEmail
from app.integrations.notify.base import DelegationMessage, Delivery, Notifier


class EmailNotifier(Notifier):
    channel = Channel.EMAIL

    def __init__(self, adapter: EmailAdapter, ceo_email: str | None = None) -> None:
        self._adapter = adapter
        self._ceo = ceo_email

    @property
    def adapter(self) -> EmailAdapter:
        return self._adapter

    def can_reach(self, delegate: Delegate) -> bool:
        return bool(delegate.email)

    def send_delegation(self, message: DelegationMessage) -> Delivery:
        sent = self._adapter.send(
            OutboundEmail(
                to=(str(message.delegate.email),),
                subject=message.subject,
                text=message.text,
                html=message.html,
            )
        )
        return Delivery(
            Channel.EMAIL,
            {
                "message_id": sent.message_id,
                "thread_id": sent.thread_id,
                "subject": message.subject,
                "source": self._adapter.source.value,
            },
        )

    def send_nudge(self, delegate: Delegate, text: str, ref: dict[str, Any]) -> None:
        subject = str(ref.get("subject") or "Delegated task")
        self._adapter.send(
            OutboundEmail(
                to=(str(delegate.email),),
                subject=f"Reminder: {subject}",
                text=f'{text}\n\nPlease reply to the original email with "Accepted".',
            )
        )

    def notify_ceo(self, text: str) -> bool:
        if not self._ceo:
            return False
        self._adapter.send(OutboundEmail(to=(self._ceo,), subject="Your assistant", text=text))
        return True
