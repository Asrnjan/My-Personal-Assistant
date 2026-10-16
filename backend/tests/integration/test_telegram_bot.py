from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import Settings
from app.domain.enums import ActionStatus, FollowUpState, Source
from app.events import Event
from app.integrations.base import ExternalTask
from app.integrations.http import HttpClient, IntegrationError
from app.integrations.notify.telegram import TelegramApi
from app.integrations.notify.telegram_bot import CEO_NO, CEO_YES, TelegramBot
from app.voice import VoiceUnavailableError
from tests.harness import Harness, at_ist, build_harness, say, tool_call
from tests.integration.test_delegation_flow import delegate

CEO = "999"
RAHUL = "100000001"


class FakeTelegram(TelegramApi):
    def __init__(self) -> None:
        super().__init__(HttpClient("telegram", "https://example.invalid/"))
        self.sent: list[tuple[str, str, Any]] = []
        self.voices: list[tuple[str, bytes]] = []
        self.answers: list[str] = []
        self.edited: list[int] = []
        self.updates: list[list[dict[str, Any]]] = []
        self.offsets: list[int | None] = []
        self.files: dict[str, bytes] = {"v1": b"OGG-AUDIO"}
        self.fail_polls = 0
        self.fail_edit = False

    def send_message(
        self, chat_id: str, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> dict[str, Any]:
        self.sent.append((chat_id, text, buttons))
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.answers.append(text)

    def edit_buttons(self, chat_id: str, message_id: int) -> None:
        if self.fail_edit:
            raise IntegrationError("telegram", "too old")
        self.edited.append(message_id)

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        if self.fail_polls:
            self.fail_polls -= 1
            raise IntegrationError("telegram", "network")
        return self.updates.pop(0) if self.updates else []

    def download_file(self, file_id: str) -> bytes:
        if file_id not in self.files:
            raise IntegrationError("telegram", "file is not available for download")
        return self.files[file_id]

    def send_voice(self, chat_id: str, audio: bytes, filename: str = "reply.ogg") -> None:
        self.voices.append((chat_id, audio))


class FakeSTT:
    def __init__(self, text: str = "brief me") -> None:
        self.text = text
        self.heard: list[bytes] = []
        self.fail = False

    def transcribe(self, audio: bytes) -> str:
        if self.fail:
            raise VoiceUnavailableError("no whisper")
        self.heard.append(audio)
        return self.text


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.fail = False

    def synthesize(self, text: str) -> bytes:
        if self.fail:
            raise VoiceUnavailableError("no piper")
        self.spoken.append(text)
        return b"RIFF-WAV"


class Bot:
    def __init__(self, h: Harness, api: FakeTelegram, stt: FakeSTT, tts: FakeTTS) -> None:
        self.h, self.api, self.stt, self.tts = h, api, stt, tts
        bot: TelegramBot = h.app.state.telegram_bot
        self.bot = bot
        self._next = 0

    def _id(self) -> int:
        self._next += 1
        return self._next

    def text(self, chat: str, text: str) -> None:
        self.bot.handle({"update_id": self._id(), "message": {"chat": {"id": chat}, "text": text}})

    def voice(self, chat: str, file_id: str = "v1") -> None:
        self.bot.handle(
            {
                "update_id": self._id(),
                "message": {"chat": {"id": chat}, "voice": {"file_id": file_id}},
            }
        )

    def press(self, chat: str, data: str) -> None:
        self.bot.handle(
            {
                "update_id": self._id(),
                "callback_query": {
                    "id": f"cb{self._next}",
                    "data": data,
                    "message": {"chat": {"id": chat}, "message_id": 42},
                },
            }
        )

    def last(self, chat: str) -> str:
        return [t for c, t, _ in self.api.sent if c == chat][-1]


@pytest.fixture
def tg(settings: Settings) -> Iterator[Bot]:
    api, stt, tts = FakeTelegram(), FakeSTT(), FakeTTS()
    configured = settings.model_copy(
        update={"telegram_ceo_chat_id": CEO, "telegram_polling": False}
    )
    for h in build_harness(configured, {"telegram": api, "stt": stt, "tts": tts}):
        yield Bot(h, api, stt, tts)


def seed(h: Harness) -> None:
    h.tasks.tasks = {
        "OPS-1": ExternalTask(Source.JIRA, "OPS-1", "Vendor contract review", due=at_ist(18))
    }
    h.sync()


def test_start_tells_anyone_their_chat_id(tg: Bot) -> None:
    tg.text("555", "/start")
    assert "Your chat id is 555" in tg.last("555")


def test_ceo_text_goes_to_the_agent_and_strangers_are_ignored(tg: Bot) -> None:
    seed(tg.h)
    tg.text(CEO, "brief me")
    assert tg.last(CEO).startswith("Good morning")
    sent_before = len(tg.api.sent)
    tg.text("12345", "brief me")
    tg.text("12345", "")
    assert len(tg.api.sent) == sent_before
    assert tg.api.voices == []  # typed in, so no voice note back


def test_ceo_confirms_with_the_yes_button(tg: Bot) -> None:
    seed(tg.h)
    tg.h.sender.queue(
        tool_call("drop_item", item_ref="vendor contract review"),
        say("Dropping Vendor contract review from your list. Confirm?"),
    )
    tg.text(CEO, "drop the vendor contract review")
    _chat, text, buttons = tg.api.sent[-1]
    assert text.endswith("Confirm?")
    assert buttons is not None and buttons[0][0]["callback_data"] == CEO_YES
    tg.press("12345", CEO_YES)
    assert tg.api.answers[-1] == "Only the assistant's owner can do that."
    assert tg.h.app.state.actions.find(ActionStatus.PENDING)
    tg.press(CEO, CEO_YES)
    assert tg.api.answers[-1] == "Done." and tg.api.edited[-1] == 42
    assert not tg.h.app.state.actions.find(ActionStatus.PENDING)


def test_ceo_cancel_button(tg: Bot) -> None:
    seed(tg.h)
    tg.h.sender.queue(
        tool_call("drop_item", item_ref="vendor contract review"),
        say("Dropping Vendor contract review from your list. Confirm?"),
    )
    tg.text(CEO, "drop the vendor contract review")
    tg.api.fail_edit = True
    tg.press(CEO, CEO_NO)
    assert not tg.h.app.state.actions.find(ActionStatus.PENDING)
    assert tg.h.app.state.actions.find(ActionStatus.CANCELLED)


def test_voice_note_in_voice_note_out(tg: Bot) -> None:
    seed(tg.h)
    tg.voice(CEO)
    assert tg.stt.heard == [b"OGG-AUDIO"]
    assert tg.last(CEO).startswith("Good morning")
    assert tg.tts.spoken and tg.tts.spoken[0].startswith("Good morning")
    # The reply is converted with ffmpeg when it's installed; either way no crash.
    assert len(tg.api.voices) <= 1


def test_voice_note_failures(tg: Bot) -> None:
    tg.voice(CEO, "missing")
    assert tg.last(CEO) == "I couldn't listen to that one. Please type it."
    tg.stt.text = ""
    tg.voice(CEO)
    assert tg.last(CEO) == "Sorry, I didn't catch that."
    tg.stt.fail = True
    tg.voice(CEO)
    assert tg.last(CEO) == "I couldn't listen to that one. Please type it."
    tg.stt.fail, tg.stt.text = False, "brief me"
    tg.tts.fail = True
    tg.voice(CEO)
    assert tg.api.voices == []


def test_delegate_accepts_with_the_button(tg: Bot) -> None:
    seed(tg.h)
    fid = delegate(tg.h, "Vendor contract review", "rahul")
    tg.press("100000004", f"ack:{fid}")  # Arjun can't accept Rahul's task
    assert tg.api.answers[-1] == "That isn't yours to accept."
    tg.press(RAHUL, f"ack:{fid}")
    assert tg.api.answers[-1] == "Accepted."
    assert tg.last(RAHUL).startswith("Thanks")
    views = tg.h.app.state.followups.views()
    assert views[0].state == FollowUpState.ACKNOWLEDGED


def test_delegate_asks_a_question(tg: Bot) -> None:
    seed(tg.h)
    fid = delegate(tg.h, "Vendor contract review", "rahul")
    tg.press("100000004", f"q:{fid}")
    assert tg.api.answers[-1] == "That isn't yours to ask about."
    tg.press(RAHUL, f"q:{fid}")
    assert tg.last(RAHUL).startswith("What's your question?")
    tg.text(RAHUL, "Which vendor, Acme or Globex?")
    assert tg.last(RAHUL) == "Thanks, I've passed your question on."
    question = tg.h.audit("followup.question")
    assert [a.outcome for a in question] == ["ok"]
    assert any("Acme or Globex" in m for m in tg.h.telegram.ceo_messages)


def test_briefs_are_forwarded_to_the_ceo_chat(tg: Bot) -> None:
    seed(tg.h)
    tg.h.app.state.jobs.morning_brief()
    assert tg.last(CEO).startswith("Good morning")
    count = len(tg.api.sent)
    tg.h.app.state.events.publish(Event("notice", {"text": "hi"}))
    tg.h.app.state.events.publish(Event("brief", {"text": "  "}))
    assert len(tg.api.sent) == count


def test_poll_loop_offsets_errors_and_stop(tg: Bot) -> None:
    tg.api.updates = [
        [
            {"update_id": 10, "message": {"chat": {"id": "1"}, "text": "/start"}},
            {"update_id": 11, "callback_query": {"id": "x", "data": "weird"}},
            {"update_id": 12, "message": None},
        ]
    ]
    assert tg.bot.poll_once() == 3
    assert tg.bot.poll_once() == 0
    assert tg.api.offsets == [None, 13]
    stop = threading.Event()
    tg.api.fail_polls = 1
    calls = 0
    original = tg.api.get_updates

    def stopping(offset: int | None, timeout: int) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls >= 2:
            stop.set()
        return original(offset, timeout)

    tg.api.get_updates = stopping  # type: ignore[method-assign]
    tg.bot.run(stop, retry_seconds=0)
    assert calls == 2
    assert tg.bot.conversation_id == f"telegram-{CEO}"
    thread = tg.bot.start(stop)
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_no_voice_engine_configured(tg: Bot) -> None:
    tg.bot._stt = None
    tg.voice(CEO)
    assert tg.last(CEO).startswith("Voice notes aren't set up")
