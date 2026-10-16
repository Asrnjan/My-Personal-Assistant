"""Microsoft Teams incoming webhooks: one card per delegation.

Incoming webhooks are send-only, so acknowledgement comes from the email the
delegate also receives (email is always worth keeping in their channels).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.domain.delegate import Delegate
from app.domain.enums import Channel
from app.integrations.http import HttpClient
from app.integrations.notify.base import DelegationMessage, Delivery, Notifier


def _card(title: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
                        {"type": "TextBlock", "text": text, "wrap": True},
                    ],
                },
            }
        ],
    }


class TeamsNotifier(Notifier):
    channel = Channel.TEAMS

    def __init__(
        self,
        ceo_webhook: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = None,
    ) -> None:
        self._ceo = ceo_webhook
        self._transport = transport
        self._sleep = sleep

    def _post(self, url: str, payload: dict[str, Any]) -> None:
        kwargs: dict[str, Any] = {"transport": self._transport}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        # No base URL: joining "" onto the webhook would add a trailing slash.
        http = HttpClient("teams", "", **kwargs)
        try:
            http.request("POST", url, json=payload)
        finally:
            http.close()

    def can_reach(self, delegate: Delegate) -> bool:
        return bool(delegate.teams_webhook)

    def send_delegation(self, message: DelegationMessage) -> Delivery:
        text = f'{message.text}\n\nReply "Accepted" to the email to accept.'
        self._post(str(message.delegate.teams_webhook), _card(message.subject, text))
        return Delivery(Channel.TEAMS, {"webhook": True})

    def send_nudge(self, delegate: Delegate, text: str, ref: dict[str, Any]) -> None:
        self._post(str(delegate.teams_webhook), _card("Reminder", text))

    def notify_ceo(self, text: str) -> bool:
        if not self._ceo:
            return False
        self._post(self._ceo, _card("Your assistant", text))
        return True
