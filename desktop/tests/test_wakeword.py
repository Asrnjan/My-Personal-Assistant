from __future__ import annotations

import queue
import sys
import threading
import time
import types
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
from conftest import FakeClock

from assistant_desktop.audio import Int16Array
from assistant_desktop.stt import Endpoint, NoiseFloor, Utterance
from assistant_desktop.wakeword import (
    TRANSITIONS,
    AudioRouter,
    InvalidTransitionError,
    OpenWakeWordDetector,
    State,
    Trigger,
    WakeWordHandler,
    default_error_text,
    phrase_to_model,
)


def utterance() -> Utterance:
    return Utterance(np.ones(1600, dtype=np.int16), 16_000, Endpoint.SILENCE)


class FakeRecorder:
    def __init__(self, results: list[Utterance | Exception | None]) -> None:
        self.results = results
        self.calls = 0

    def record(self) -> Utterance | None:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeTranscriber:
    def __init__(self, texts: list[str | Exception]) -> None:
        self.texts = texts

    def transcribe(self, audio: Any, sample_rate: int) -> str:
        assert sample_rate == 16_000
        text = self.texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return text


class FakeSpeaker:
    def __init__(self) -> None:
        self.spoken: list[tuple[str, bool]] = []
        self.stops = 0
        self.during_speak: Callable[[str], None] | None = None
        self.fail_on: set[str] = set()

    def speak(self, text: str, *, chime: bool = False) -> bool:
        self.spoken.append((text, chime))
        if any(marker in text for marker in self.fail_on):
            raise RuntimeError("audio device lost")
        if self.during_speak is not None:
            hook, self.during_speak = self.during_speak, None
            hook(text)
        return True

    def stop(self) -> None:
        self.stops += 1


class Harness:
    def __init__(
        self,
        clock: FakeClock,
        *,
        recordings: list[Utterance | Exception | None] | None = None,
        texts: list[str | Exception] | None = None,
        voice: bool = True,
        ask: Callable[[str], str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.states: list[tuple[State, str]] = []
        self.messages: list[tuple[str, str]] = []
        self.asked: list[str] = []
        self.speaker = FakeSpeaker()
        self.recorder = FakeRecorder(recordings or [])
        self.transcriber = FakeTranscriber(texts or [])

        def default_ask(text: str) -> str:
            self.asked.append(text)
            return f"reply to {text}"

        self.handler = WakeWordHandler(
            ask=ask or default_ask,
            speaker=self.speaker,
            recorder=self.recorder if voice else None,
            transcriber=self.transcriber if voice else None,
            cooldown_seconds=1.0,
            clock=clock,
            on_state=lambda s, d: self.states.append((s, d)),
            on_message=lambda r, t: self.messages.append((r, t)),
            **kwargs,
        )

    def drain(self) -> None:
        while self.handler.process_next():
            continue

    @property
    def state_names(self) -> list[State]:
        return [s for s, _ in self.states]


def test_transition_table_is_closed() -> None:
    assert set(TRANSITIONS) == set(State)
    for targets in TRANSITIONS.values():
        assert targets <= set(State)


def test_full_turn_via_hotkey(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance()], texts=[" What's next? "])
    assert h.handler.trigger(Trigger.HOTKEY) is True
    assert h.handler.state is State.LISTENING
    h.drain()
    assert h.handler.state is State.IDLE
    assert h.states == [
        (State.LISTENING, "hotkey"),
        (State.THINKING, "transcribing"),
        (State.THINKING, "thinking"),
        (State.SPEAKING, "replying"),
        (State.IDLE, "done"),
    ]
    assert h.asked == ["What's next?"]
    assert h.messages == [("you", "What's next?"), ("assistant", "reply to What's next?")]
    assert h.speaker.spoken == [("reply to What's next?", False)]


def test_wake_word_starts_turn(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance()], texts=["hi"])
    assert h.handler.trigger(Trigger.WAKE_WORD) is True
    assert h.states[0] == (State.LISTENING, "wake word")
    h.drain()
    assert h.handler.state is State.IDLE


def test_no_voice_input(clock: FakeClock) -> None:
    h = Harness(clock, voice=False)
    assert h.handler.voice_input is False
    assert h.handler.trigger(Trigger.HOTKEY) is False
    assert h.messages[0][0] == "info"
    assert h.handler.state is State.IDLE


def test_listen_timeout_returns_to_idle(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[None])
    h.handler.trigger(Trigger.HOTKEY)
    h.drain()
    assert h.state_names == [State.LISTENING, State.IDLE]
    assert h.states[-1] == (State.IDLE, "timeout")
    assert h.messages == [("info", "No speech heard.")]
    assert h.speaker.spoken == []


def test_empty_transcript_returns_to_idle(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance()], texts=["   "])
    h.handler.trigger(Trigger.HOTKEY)
    h.drain()
    assert h.state_names == [State.LISTENING, State.THINKING, State.IDLE]
    assert h.asked == []


@pytest.mark.parametrize("stage", ["record", "transcribe", "ask"])
def test_errors_return_to_idle_and_are_spoken(clock: FakeClock, stage: str) -> None:
    boom = RuntimeError(f"{stage} failed")

    def ask(_text: str) -> str:
        raise boom

    h = Harness(
        clock,
        recordings=[boom if stage == "record" else utterance()],
        texts=[boom if stage == "transcribe" else "hello"],
        ask=ask if stage == "ask" else None,
        error_text=lambda exc: f"friendly {exc}",
    )
    h.handler.trigger(Trigger.HOTKEY)
    h.drain()
    assert h.handler.state is State.IDLE
    assert ("error", f"friendly {stage} failed") in h.messages
    assert h.speaker.spoken == [(f"Sorry. friendly {stage} failed", False)]
    assert h.states[-3:] == [
        (State.IDLE, "error"),
        (State.SPEAKING, "error"),
        (State.IDLE, "error"),
    ]


def test_speaker_failure_is_not_spoken_again(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance()], texts=["hello"])
    h.speaker.fail_on = {"reply"}
    h.handler.trigger(Trigger.HOTKEY)
    h.drain()
    assert h.handler.state is State.IDLE
    assert h.speaker.spoken == [("reply to hello", False)]
    assert h.messages[-1] == ("error", "audio device lost")


def test_failure_while_speaking_the_error(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[RuntimeError("mic")])
    h.speaker.fail_on = {"Sorry"}
    h.handler.trigger(Trigger.HOTKEY)
    h.drain()
    assert h.handler.state is State.IDLE


def test_cooldown_blocks_double_triggers(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[None, None, None])  # cooldown 1.0 s
    assert h.handler.trigger(Trigger.HOTKEY)
    h.drain()  # no speech -> IDLE immediately
    assert h.handler.state is State.IDLE
    clock.advance(0.5)
    assert h.handler.trigger(Trigger.HOTKEY) is False  # within cooldown of last trigger
    clock.advance(0.6)
    assert h.handler.trigger(Trigger.HOTKEY) is True
    h.drain()
    # A wake word right after returning to idle is treated as an echo.
    clock.advance(1.5)
    h.handler._idle_since = clock()
    clock.advance(0.2)
    assert h.handler.trigger(Trigger.WAKE_WORD) is False
    assert h.handler.trigger(Trigger.HOTKEY) is True  # the hotkey is deliberate


def test_triggers_ignored_while_busy(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance()], texts=["hello"])
    h.handler.trigger(Trigger.HOTKEY)
    clock.advance(5)
    assert h.handler.trigger(Trigger.HOTKEY) is False  # already listening
    assert h.handler.submit_text("typed") is False
    seen: list[bool] = []

    def wake_while_speaking(_text: str) -> None:
        clock.advance(5)
        seen.append(h.handler.trigger(Trigger.WAKE_WORD))

    h.speaker.during_speak = wake_while_speaking
    h.drain()
    assert seen == [False]  # its own voice must not wake it


def test_barge_in_stops_playback_and_listens(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance(), utterance()], texts=["first", "second"])
    h.handler.trigger(Trigger.HOTKEY)
    barged: list[bool] = []

    def barge(_text: str) -> None:
        clock.advance(2)
        barged.append(h.handler.trigger(Trigger.HOTKEY))
        assert h.handler.state is State.LISTENING

    h.speaker.during_speak = barge
    h.drain()
    assert barged == [True]
    assert h.speaker.stops == 1
    assert h.asked == ["first", "second"]
    assert (State.LISTENING, "barge-in") in h.states
    names = h.state_names
    i = names.index(State.SPEAKING)
    assert names[i + 1] is State.LISTENING
    assert h.handler.state is State.IDLE
    assert h.recorder.calls == 2


def test_typed_text_skips_listening(clock: FakeClock) -> None:
    h = Harness(clock, voice=False)
    assert h.handler.submit_text("  ") is False
    assert h.handler.submit_text("brief me") is True
    assert h.handler.state is State.THINKING
    h.drain()
    assert h.asked == ["brief me"]
    assert h.state_names == [State.THINKING, State.THINKING, State.SPEAKING, State.IDLE]
    assert h.messages[0] == ("you", "brief me")


def test_announcement_chimes_then_speaks(clock: FakeClock) -> None:
    h = Harness(clock)
    h.handler.announce("Your 9 am moved to 10.", "alert")
    h.drain()
    assert h.speaker.spoken == [("Your 9 am moved to 10.", True)]
    assert h.messages == [("alert", "Your 9 am moved to 10.")]
    assert h.states == [(State.SPEAKING, "alert"), (State.IDLE, "done")]


def test_announcement_without_chime(clock: FakeClock) -> None:
    h = Harness(clock, chime=False)
    h.handler.announce("Brief.", "brief")
    h.drain()
    assert h.speaker.spoken == [("Brief.", False)]


def test_announcement_waits_for_current_turn(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance()], texts=["hello"])
    h.handler.announce("Brief ready.", "brief")
    h.handler.trigger(Trigger.HOTKEY)  # turn starts before the brief is played
    assert h.handler.process_next() is True  # brief deferred: we are LISTENING
    assert h.speaker.spoken == []
    h.drain()
    assert [t for t, _ in h.speaker.spoken] == ["reply to hello", "Brief ready."]


def test_barge_in_during_announcement(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance()], texts=["stop that"])

    def barge(_text: str) -> None:
        clock.advance(2)
        h.handler.trigger(Trigger.HOTKEY)

    h.speaker.during_speak = barge
    h.handler.announce("Long brief.", "brief")
    h.drain()
    assert h.asked == ["stop that"]
    assert h.handler.state is State.IDLE


def test_invalid_transition_raises(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[None])
    h.handler.trigger(Trigger.HOTKEY)
    with pytest.raises(InvalidTransitionError, match="listening -> speaking"):
        h.handler._transition(State.SPEAKING)
    h.handler._transition(State.LISTENING)  # same state: no-op


def test_listener_failures_are_contained(clock: FakeClock) -> None:
    def explode(*_args: Any) -> None:
        raise RuntimeError("listener bug")

    handler = WakeWordHandler(
        ask=lambda t: t,
        speaker=FakeSpeaker(),
        clock=clock,
        on_state=explode,
        on_message=explode,
    )
    handler.add_listener(explode)
    handler.add_message_listener(explode)
    assert handler.submit_text("hi")
    while handler.process_next():
        continue
    assert handler.state is State.IDLE


def test_unexpected_job_failure_recovers(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(clock)

    def broken(_job: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(h.handler, "_run", broken)
    h.handler.announce("x")
    assert h.handler.process_next() is True
    assert ("error", "bug") in h.messages
    assert h.handler.state is State.IDLE


def test_listen_loop_without_voice_input_goes_idle(clock: FakeClock) -> None:
    h = Harness(clock, voice=False)
    h.handler._set_state(State.LISTENING, "forced")
    h.handler._listen_loop()
    assert h.handler.state is State.IDLE


def test_worker_thread(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[utterance()], texts=["threaded"])
    h.handler.start()
    try:
        assert h.handler.process_next(timeout=0.01) is False
        h.handler.trigger(Trigger.HOTKEY)
        deadline = time.monotonic() + 2
        while not h.speaker.spoken and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        h.handler.stop()
    assert h.speaker.spoken == [("reply to threaded", False)]
    assert h.speaker.stops >= 1


def test_default_error_text() -> None:
    assert default_error_text(RuntimeError("x")) == "x"
    assert default_error_text(KeyError()) == "KeyError"


# -- wake word detection ----------------------------------------------------


def test_phrase_to_model() -> None:
    assert phrase_to_model("Hey Jarvis") == "hey_jarvis"
    assert phrase_to_model("  hey, mycroft! ") == "hey_mycroft"
    assert phrase_to_model("/models/custom.onnx") == "/models/custom.onnx"


class FakeOwwModel:
    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.resets = 0
        self.frames: list[Int16Array] = []

    def predict(self, frame: Int16Array) -> dict[str, float]:
        self.frames.append(frame)
        return {"hey_jarvis": self.scores.pop(0)} if self.scores else {}

    def reset(self) -> None:
        self.resets += 1


def test_detector_threshold_and_lazy_load() -> None:
    model = FakeOwwModel([0.2, 0.6, 0.9])
    names: list[str] = []

    def factory(name: str) -> FakeOwwModel:
        names.append(name)
        return model

    detector = OpenWakeWordDetector("Hey Jarvis", threshold=0.6, model_factory=factory)
    detector.reset()  # before load: nothing to reset
    frame = np.zeros(1280, dtype=np.int16)
    assert [detector.process(frame) for _ in range(3)] == [False, True, True]
    assert detector.process(frame) is False  # no scores
    assert names == ["hey_jarvis"]
    detector.reset()
    assert model.resets == 1


def test_detector_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    downloads: list[list[str]] = []
    created: list[dict[str, Any]] = []

    class Model:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    package = types.ModuleType("openwakeword")
    package.utils = types.SimpleNamespace(  # type: ignore[attr-defined]
        download_models=lambda model_names: downloads.append(model_names)
    )
    model_module = types.ModuleType("openwakeword.model")
    model_module.Model = Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openwakeword", package)
    monkeypatch.setitem(sys.modules, "openwakeword.model", model_module)

    OpenWakeWordDetector("hey jarvis").load()
    OpenWakeWordDetector("/m/custom.tflite").load()
    assert downloads == [["hey_jarvis"]]
    assert created == [
        {"wakeword_models": ["hey_jarvis"], "inference_framework": "onnx"},
        {"wakeword_models": ["/m/custom.tflite"], "inference_framework": "tflite"},
    ]


def test_detector_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "openwakeword", None)
    with pytest.raises(RuntimeError, match="openwakeword"):
        OpenWakeWordDetector().load()


# -- audio router -----------------------------------------------------------


class ListSource:
    def __init__(self, frames: list[Int16Array]) -> None:
        self.frames = frames

    def read(self, timeout: float | None = None) -> Int16Array | None:
        return self.frames.pop(0) if self.frames else None


class ScriptedDetector:
    def __init__(self, fire_on: set[int], fail_on: int | None = None) -> None:
        self.fire_on = fire_on
        self.fail_on = fail_on
        self.seen = 0
        self.resets = 0

    def process(self, frame: Int16Array) -> bool:
        self.seen += 1
        if self.seen == self.fail_on:
            raise RuntimeError("model crashed")
        return self.seen in self.fire_on

    def reset(self) -> None:
        self.resets += 1


def frame(value: int) -> Int16Array:
    return np.full(1280, value, dtype=np.int16)


def test_router_wakes_and_feeds_recorder(clock: FakeClock) -> None:
    h = Harness(clock, recordings=[None])
    detector = ScriptedDetector(fire_on={2})
    noise = NoiseFloor()
    listen: queue.Queue[Int16Array] = queue.Queue()
    source = ListSource([frame(1), frame(2), frame(3), frame(4)])
    router = AudioRouter(source, h.handler, listen, detector, noise)
    for _ in range(4):
        router.pump()
    assert h.handler.state is State.LISTENING
    assert detector.seen == 2
    assert detector.resets == 1  # reset after firing
    assert [int(listen.get_nowait()[0]) for _ in range(2)] == [3, 4]
    assert noise.value < -60  # learnt from the two idle frames
    router.pump()  # no frame available
    h.drain()
    source.frames.append(frame(5))
    router.pump()
    assert detector.resets == 2  # back to idle: detector state cleared


def test_router_drops_frames_while_speaking(clock: FakeClock) -> None:
    h = Harness(clock)
    h.handler._set_state(State.SPEAKING, "forced")
    detector = ScriptedDetector(fire_on={1})
    listen: queue.Queue[Int16Array] = queue.Queue()
    router = AudioRouter(ListSource([frame(1)]), h.handler, listen, detector)
    router.pump()
    assert detector.seen == 0
    assert listen.empty()


def test_router_disables_broken_detector(clock: FakeClock) -> None:
    h = Harness(clock)
    detector = ScriptedDetector(fire_on=set(), fail_on=1)
    router = AudioRouter(ListSource([frame(1), frame(2)]), h.handler, queue.Queue(), detector)
    router.pump()
    router.pump()
    assert detector.seen == 1


def test_router_run_and_stop(clock: FakeClock) -> None:
    h = Harness(clock)
    router = AudioRouter(ListSource([]), h.handler, queue.Queue())

    def stop_soon() -> None:
        time.sleep(0.02)
        router.stop()

    stopper = threading.Thread(target=stop_soon)
    stopper.start()
    router.run()
    stopper.join()
