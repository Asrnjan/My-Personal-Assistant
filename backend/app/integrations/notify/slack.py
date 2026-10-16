"""Slack: a direct message from the assistant's bot, with acknowledgement by
reply ("accepted", "ok", "on it") or a ✅ / 👍 reaction, found by polling."""

from __future__ import annotations

import re
from typing import Any

from app.domain.delegate import Delegate
from app.domain.enums import Channel
from app.integrations.http import HttpClient, IntegrationError
from app.integrations.notify.base import (
    AckEvent,
    AckSignal,
    DelegationMessage,
    Delivery,
    Notifier,
)

BASE_URL = "https://slack.com/api/"
ACK_REACTIONS = frozenset({"white_check_mark", "heavy_check_mark", "+1", "thumbsup", "ok"})
_ACK_WORDS = re.compile(r"\b(accept(ed)?|ok(ay)?|on it|will do|got it)\b", re.IGNORECASE)


def slack_client(token: str, **kwargs: Any) -> HttpClient:
    return HttpClient(
        "slack", BASE_URL, auth_headers=lambda: {"Authorization": f"Bearer {token}"}, **kwargs
    )


class SlackNotifier(Notifier):
    channel = Channel.SLACK

    def __init__(self, http: HttpClient, ceo_user_id: str | None = None) -> None:
        self._http = http
        self._ceo = ceo_user_id

    def _call(self, method: str, body: dict[str, Any], *, get: bool = False) -> dict[str, Any]:
        if get:
            data = self._http.get_json(method, params=body)
        else:
            data = self._http.send_json("POST", method, body)
        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else data
            raise IntegrationError("slack", f"{method} failed: {error}")
        return data

    def _dm(self, user_id: str) -> str:
        opened = self._call("conversations.open", {"users": user_id})
        return str(opened["channel"]["id"])

    def can_reach(self, delegate: Delegate) -> bool:
        return bool(delegate.slack_user_id)

    def send_delegation(self, message: DelegationMessage) -> Delivery:
        channel = self._dm(str(message.delegate.slack_user_id))
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": message.subject[:150]}},
            {"type": "section", "text": {"type": "plain_text", "text": message.text[:2900]}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Reply *accepted* or react with :white_check_mark: to accept.",
                    }
                ],
            },
        ]
        posted = self._call(
            "chat.postMessage",
            {"channel": channel, "text": message.subject, "blocks": blocks},
        )
        return Delivery(
            Channel.SLACK,
            {"channel": channel, "ts": posted.get("ts"), "user": message.delegate.slack_user_id},
        )

    def send_nudge(self, delegate: Delegate, text: str, ref: dict[str, Any]) -> None:
        body: dict[str, Any] = {"channel": ref.get("channel"), "text": text}
        if ref.get("ts"):
            body["thread_ts"] = ref["ts"]
        self._call("chat.postMessage", body)

    def notify_ceo(self, text: str) -> bool:
        if not self._ceo:
            return False
        self._call("chat.postMessage", {"channel": self._dm(self._ceo), "text": text})
        return True

    def poll_ack(self, ref: dict[str, Any], seen: set[str]) -> list[AckEvent]:
        channel, ts, user = ref.get("channel"), ref.get("ts"), ref.get("user")
        if not channel or not ts:
            return []
        events: list[AckEvent] = []
        reaction_id = f"reaction:{ts}"
        if reaction_id not in seen:
            message = self._call(
                "reactions.get", {"channel": channel, "timestamp": ts}, get=True
            ).get("message", {})
            for reaction in message.get("reactions", []):
                if reaction.get("name") in ACK_REACTIONS and (
                    not user or user in reaction.get("users", [])
                ):
                    return [AckEvent("acknowledged", "reacted", reaction_id)]
        history = self._call(
            "conversations.history", {"channel": channel, "oldest": ts, "limit": 50}, get=True
        )
        for msg in history.get("messages", []):
            msg_ts = str(msg.get("ts"))
            if msg_ts == ts or msg_ts in seen or (user and msg.get("user") != user):
                continue
            text = str(msg.get("text", ""))
            signal: AckSignal = "acknowledged" if _ACK_WORDS.search(text) else "question"
            events.append(AckEvent(signal, text, msg_ts))
        return events
