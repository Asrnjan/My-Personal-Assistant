"""Telegram Bot API: delegate notifications with Accept / Question buttons, CEO
notices, and the low-level calls the CEO's bot loop uses.

A delegate must open the bot and press Start once before it can message them
(Telegram bots can't start conversations); see docs/SETUP.md.
"""

from __future__ import annotations

from typing import Any

from app.domain.delegate import Delegate
from app.domain.enums import Channel
from app.integrations.http import HttpClient, IntegrationError
from app.integrations.notify.base import DelegationMessage, Delivery, Notifier

BASE_URL = "https://api.telegram.org"
# Telegram limits a message to 4096 characters.
MAX_TEXT = 4000

ACK_PREFIX = "ack:"
QUESTION_PREFIX = "q:"


def bot_client(token: str, *, files: bool = False, **kwargs: Any) -> HttpClient:
    """Client for Bot API methods, or (``files=True``) for downloading files."""
    prefix = "file/bot" if files else "bot"
    return HttpClient("telegram", f"{BASE_URL}/{prefix}{token}/", **kwargs)


class TelegramApi:
    """The handful of Bot API methods the assistant uses."""

    def __init__(self, http: HttpClient, file_http: HttpClient | None = None) -> None:
        self._http = http
        self._files = file_http

    def _call(self, method: str, body: dict[str, Any]) -> Any:
        data = self._http.send_json("POST", method, body)
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else data
            raise IntegrationError("telegram", f"{method} failed: {description}")
        return data.get("result")

    def send_message(
        self, chat_id: str, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MAX_TEXT],
            "disable_web_page_preview": True,
        }
        if buttons:
            body["reply_markup"] = {"inline_keyboard": buttons}
        result: dict[str, Any] = self._call("sendMessage", body)
        return result

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def edit_buttons(self, chat_id: str, message_id: int) -> None:
        """Remove the inline buttons once they have been used."""
        self._call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            body["offset"] = offset
        result: list[dict[str, Any]] = self._call("getUpdates", body) or []
        return result

    def download_file(self, file_id: str) -> bytes:
        info = self._call("getFile", {"file_id": file_id})
        path = info.get("file_path") if isinstance(info, dict) else None
        if not path or self._files is None:
            raise IntegrationError("telegram", "file is not available for download")
        return self._files.request("GET", path).content

    def send_voice(self, chat_id: str, audio: bytes, filename: str = "reply.ogg") -> None:
        response = self._http.request(
            "POST",
            "sendVoice",
            data={"chat_id": chat_id},
            files={"voice": (filename, audio, "audio/ogg")},
        )
        data = response.json()
        if not data.get("ok"):
            raise IntegrationError("telegram", f"sendVoice failed: {data.get('description')}")


class TelegramNotifier(Notifier):
    channel = Channel.TELEGRAM

    def __init__(self, api: TelegramApi, ceo_chat_id: str | None = None) -> None:
        self._api = api
        self._ceo = ceo_chat_id

    def can_reach(self, delegate: Delegate) -> bool:
        return bool(delegate.telegram_chat_id)

    def send_delegation(self, message: DelegationMessage) -> Delivery:
        chat = str(message.delegate.telegram_chat_id)
        text = f"{message.subject}\n\n{message.text}"
        buttons = [
            [
                {"text": "✅ Accept", "callback_data": f"{ACK_PREFIX}{message.followup_id}"},
                {"text": "❓ Question", "callback_data": f"{QUESTION_PREFIX}{message.followup_id}"},
            ]
        ]
        sent = self._api.send_message(chat, text, buttons)
        return Delivery(Channel.TELEGRAM, {"chat_id": chat, "message_id": sent.get("message_id")})

    def send_nudge(self, delegate: Delegate, text: str, ref: dict[str, Any]) -> None:
        self._api.send_message(str(ref.get("chat_id") or delegate.telegram_chat_id), text)

    def notify_ceo(self, text: str) -> bool:
        if not self._ceo:
            return False
        self._api.send_message(self._ceo, text)
        return True
