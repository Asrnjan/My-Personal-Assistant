"""The Telegram bot loop: long-polls ``getUpdates`` and handles

* delegates pressing **Accept** / **Question** under a delegation,
* the CEO talking to the assistant by text or voice note (replies come back as
  text, plus a voice note when the CEO spoke), and **Yes** / **Cancel** buttons
  under anything that needs confirmation,
* ``/start`` from anyone, answered with their chat id for ``contacts.yaml`` or
  ``TELEGRAM_CEO_CHAT_ID``.

Only the configured CEO chat can talk to the assistant; a button press only
counts when it comes from the delegate's own chat.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from app.domain.delegate import Delegate
from app.domain.enums import ApprovedVia
from app.engine.conversation import ConversationAgent, ConverseResponse, spoken_reply
from app.engine.followup import FollowUpService
from app.events import Event, EventBus
from app.integrations.http import IntegrationError
from app.integrations.notify.telegram import ACK_PREFIX, QUESTION_PREFIX, TelegramApi
from app.voice import SpeechToText, TextToSpeech, VoiceUnavailableError, wav_to_ogg

log = logging.getLogger(__name__)

CEO_YES = "ceo:yes"
CEO_NO = "ceo:no"
POLL_TIMEOUT = 25
_CONFIRM_BUTTONS = [
    [
        {"text": "✅ Yes", "callback_data": CEO_YES},
        {"text": "✖ Cancel", "callback_data": CEO_NO},
    ]
]


def _chat_id(update_part: dict[str, Any]) -> str:
    chat = update_part.get("chat") or {}
    return str(chat.get("id", ""))


class TelegramBot:
    def __init__(
        self,
        api: TelegramApi,
        ceo_chat_id: str,
        agent: ConversationAgent,
        followups: FollowUpService,
        *,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
    ) -> None:
        self._api = api
        self._ceo = str(ceo_chat_id)
        self._agent = agent
        self._followups = followups
        self._stt = stt
        self._tts = tts
        self._offset: int | None = None
        # chat id -> follow-up id, for a delegate who pressed "Question" and is typing it.
        self._asking: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def conversation_id(self) -> str:
        return f"telegram-{self._ceo}"

    # -- loop -----------------------------------------------------------------------

    def poll_once(self, timeout: int = POLL_TIMEOUT) -> int:
        """Fetch and handle one batch of updates. Returns how many were handled."""
        updates = self._api.get_updates(self._offset, timeout)
        for update in updates:
            self._offset = int(update.get("update_id", 0)) + 1
            try:
                self.handle(update)
            except Exception:
                log.exception("telegram update %s failed", update.get("update_id"))
        return len(updates)

    def run(self, stop: threading.Event, retry_seconds: float = 10.0) -> None:
        log.info("telegram bot polling")
        while not stop.is_set():
            try:
                self.poll_once()
            except IntegrationError as exc:
                log.warning("telegram polling failed, retrying: %s", exc)
                stop.wait(retry_seconds)

    def start(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.run, args=(stop,), name="telegram-bot", daemon=True)
        thread.start()
        return thread

    def on_event(self, event: Event) -> None:
        """Bus subscriber: send scheduled briefs and alerts to the CEO chat."""
        if event.kind != "brief" or not self._ceo:
            return
        text = str(event.payload.get("text", "")).strip()
        if text:
            self._api.send_message(self._ceo, text)

    def subscribe(self, bus: EventBus) -> Callable[[], None]:
        """Returns the function that unsubscribes again."""
        return bus.subscribe(self.on_event)

    # -- dispatch ---------------------------------------------------------------------

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._callback(update["callback_query"])
        elif "message" in update:
            self._message(update["message"])

    def _callback(self, query: dict[str, Any]) -> None:
        data = str(query.get("data", ""))
        message = query.get("message") or {}
        chat = _chat_id(message) or str((query.get("from") or {}).get("id", ""))
        answer = "Done."
        if data in (CEO_YES, CEO_NO):
            if chat != self._ceo:
                answer = "Only the assistant's owner can do that."
            else:
                self._converse("yes" if data == CEO_YES else "cancel", voice=False)
        elif data.startswith(ACK_PREFIX) or data.startswith(QUESTION_PREFIX):
            answer = self._delegate_button(data, chat)
        self._api.answer_callback(str(query.get("id", "")), answer)
        if message.get("message_id") is not None:
            try:
                self._api.edit_buttons(chat, int(message["message_id"]))
            except IntegrationError as exc:
                log.info("could not remove telegram buttons: %s", exc)

    def _delegate_button(self, data: str, chat: str) -> str:
        def is_sender(delegate: Delegate) -> bool:
            return bool(delegate.telegram_chat_id) and str(delegate.telegram_chat_id) == chat

        if data.startswith(ACK_PREFIX):
            ok = self._followups.acknowledge(
                data[len(ACK_PREFIX) :], "telegram", sender_check=is_sender
            )
            if ok:
                self._api.send_message(chat, "Thanks, I've let them know you've got it.")
                return "Accepted."
            return "That isn't yours to accept."
        followup_id = data[len(QUESTION_PREFIX) :]
        if not self._followups.is_delegate(followup_id, is_sender):
            return "That isn't yours to ask about."
        with self._lock:
            self._asking[chat] = followup_id
        self._api.send_message(chat, "What's your question? Reply here and I'll pass it on.")
        return "Ask away."

    def _message(self, message: dict[str, Any]) -> None:
        chat = _chat_id(message)
        text = str(message.get("text", "")).strip()
        if text.startswith("/start"):
            self._api.send_message(
                chat,
                f"Hello. Your chat id is {chat}. Give it to the assistant's owner so they can "
                "reach you here.",
            )
            return
        with self._lock:
            asking = self._asking.pop(chat, None)
        if asking is not None and text:

            def is_sender(delegate: Delegate) -> bool:
                return str(delegate.telegram_chat_id or "") == chat

            if self._followups.question(asking, "telegram", text, sender_check=is_sender):
                self._api.send_message(chat, "Thanks, I've passed your question on.")
            return
        if chat != self._ceo or not self._ceo:
            return
        voice = message.get("voice") or message.get("audio")
        if voice:
            self._voice_message(str(voice.get("file_id", "")))
        elif text:
            self._converse(text, voice=False)

    def _voice_message(self, file_id: str) -> None:
        if self._stt is None:
            self._api.send_message(self._ceo, "Voice notes aren't set up here yet. Please type it.")
            return
        try:
            heard = self._stt.transcribe(self._api.download_file(file_id))
        except (VoiceUnavailableError, IntegrationError) as exc:
            log.warning("telegram voice note not transcribed: %s", exc)
            self._api.send_message(self._ceo, "I couldn't listen to that one. Please type it.")
            return
        if not heard:
            self._api.send_message(self._ceo, "Sorry, I didn't catch that.")
            return
        self._converse(heard, voice=True)

    def _converse(self, text: str, *, voice: bool) -> ConverseResponse:
        response = self._agent.converse(text, self.conversation_id, ApprovedVia.TELEGRAM)
        buttons = _CONFIRM_BUTTONS if response.needs_confirmation else None
        self._api.send_message(self._ceo, response.reply, buttons)
        if voice and self._tts is not None:
            self._send_voice(response.reply)
        return response

    def _send_voice(self, reply: str) -> None:
        assert self._tts is not None  # noqa: S101 - checked by the caller
        try:
            wav = self._tts.synthesize(spoken_reply(reply))
        except VoiceUnavailableError as exc:
            log.warning("no voice reply: %s", exc)
            return
        ogg = wav_to_ogg(wav)
        if ogg is not None:
            self._api.send_voice(self._ceo, ogg)
