"""The voice loop: wake word detection and the conversation state machine.

States and transitions (anything else raises :class:`InvalidTransitionError`)::

    IDLE ──wake word / hotkey──▶ LISTENING ──end of speech──▶ THINKING ──reply──▶ SPEAKING
      ▲  ◀──────no speech─────────┘   ▲          │ (STT + backend)                 │
      │  ◀──────────────empty transcript / error─┘                                 │
      ├──typed text──▶ THINKING        └───────────hotkey (barge-in)───────────────┤
      ├──pushed brief / alert──▶ SPEAKING                                          │
      └◀───────────────────────────────playback finished───────────────────────────┘

Any error returns to IDLE and the error is shown and spoken. Triggers inside
the cooldown window are ignored, and so is a wake word right after the
assistant stopped speaking (its own voice must not wake it up again).

Work runs on one worker thread, fed by a job queue; :meth:`WakeWordHandler.trigger`
is safe to call from the hotkey, tray and audio threads.
"""

from __future__ import annotations

import enum
import logging
import math
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from numpy.typing import NDArray

from assistant_desktop.audio import Int16Array
from assistant_desktop.stt import NoiseFloor, Utterance

log = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Trigger(enum.Enum):
    WAKE_WORD = "wake word"
    HOTKEY = "hotkey"


TRANSITIONS: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.LISTENING, State.THINKING, State.SPEAKING}),
    State.LISTENING: frozenset({State.THINKING, State.IDLE}),
    State.THINKING: frozenset({State.SPEAKING, State.IDLE}),
    State.SPEAKING: frozenset({State.IDLE, State.LISTENING}),
}


class InvalidTransitionError(RuntimeError):
    """A transition the state machine does not allow."""


class Recorder(Protocol):
    def record(self) -> Utterance | None: ...


class Transcriber(Protocol):
    def transcribe(self, audio: NDArray[Any], sample_rate: int) -> str: ...


class Speaker(Protocol):
    def speak(self, text: str, *, chime: bool = False) -> bool: ...

    def stop(self) -> None: ...


StateListener = Callable[[State, str], None]
MessageListener = Callable[[str, str], None]
"""Called with (role, text); roles: you, assistant, brief, alert, notice, info, error."""


@dataclass(frozen=True)
class _Job:
    kind: Literal["turn", "text", "announce"]
    text: str = ""
    label: str = ""


def default_error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class WakeWordHandler:
    def __init__(
        self,
        *,
        ask: Callable[[str], str],
        speaker: Speaker,
        recorder: Recorder | None = None,
        transcriber: Transcriber | None = None,
        cooldown_seconds: float = 1.5,
        chime: bool = True,
        clock: Callable[[], float] = time.monotonic,
        on_state: StateListener | None = None,
        on_message: MessageListener | None = None,
        error_text: Callable[[BaseException], str] = default_error_text,
    ) -> None:
        self._ask = ask
        self._speaker = speaker
        self._recorder = recorder
        self._transcriber = transcriber
        self.cooldown_seconds = cooldown_seconds
        self.chime = chime
        self._clock = clock
        self._listeners: list[StateListener] = [on_state] if on_state else []
        self._message_listeners: list[MessageListener] = [on_message] if on_message else []
        self._error_text = error_text
        self._state = State.IDLE
        self._lock = threading.RLock()
        self._last_trigger = -math.inf
        self._idle_since = -math.inf
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._deferred: list[_Job] = []
        self._stopped = threading.Event()
        self._worker: threading.Thread | None = None

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def voice_input(self) -> bool:
        return self._recorder is not None and self._transcriber is not None

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def _set_state(self, new: State, detail: str) -> bool:
        """Change state under the lock; returns False when already there."""
        with self._lock:
            old = self._state
            if new is old:
                return False
            if new not in TRANSITIONS[old]:
                raise InvalidTransitionError(f"{old.value} -> {new.value}")
            self._state = new
            if new is State.IDLE:
                self._idle_since = self._clock()
            log.debug("state %s -> %s (%s)", old.value, new.value, detail)
            return True

    def _notify(self, state: State, detail: str) -> None:
        for listener in list(self._listeners):
            try:
                listener(state, detail)
            except Exception:
                log.exception("state listener failed")

    def _transition(self, new: State, detail: str = "") -> None:
        if self._set_state(new, detail):
            self._notify(new, detail)

    def add_message_listener(self, listener: MessageListener) -> None:
        self._message_listeners.append(listener)

    def _message(self, role: str, text: str) -> None:
        for listener in list(self._message_listeners):
            try:
                listener(role, text)
            except Exception:
                log.exception("message listener failed")

    # -- inputs (any thread) ------------------------------------------------

    def trigger(self, source: Trigger) -> bool:
        """Wake word or hotkey; returns True when it started (or barged into) a turn."""
        if not self.voice_input:
            self._message("info", "Voice input is not available; type your request instead.")
            return False
        with self._lock:
            now = self._clock()
            if now - self._last_trigger < self.cooldown_seconds:
                return False
            state = self._state
            if state is State.IDLE:
                if source is Trigger.WAKE_WORD and now - self._idle_since < self.cooldown_seconds:
                    return False
                action = "turn"
            elif state is State.SPEAKING and source is Trigger.HOTKEY:
                action = "barge-in"
            else:
                return False
            self._last_trigger = now
            self._set_state(State.LISTENING, source.value)
        self._notify(State.LISTENING, "barge-in" if action == "barge-in" else source.value)
        if action == "barge-in":
            self._speaker.stop()
        else:
            self._jobs.put(_Job("turn"))
        return True

    def submit_text(self, text: str) -> bool:
        """A typed request; skips listening and speech-to-text."""
        text = text.strip()
        if not text:
            return False
        with self._lock:
            if self._state is not State.IDLE:
                return False
            self._set_state(State.THINKING, "typed")
        self._notify(State.THINKING, "typed")
        self._jobs.put(_Job("text", text))
        return True

    def announce(self, text: str, kind: str = "notice") -> None:
        """A pushed brief, alert or notice: chime, then speak it when free."""
        self._jobs.put(_Job("announce", text, kind))

    # -- worker ------------------------------------------------------------

    def start(self) -> None:
        self._stopped.clear()
        self._worker = threading.Thread(target=self._work, name="voice-loop", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stopped.set()
        self._speaker.stop()
        self._jobs.put(None)
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=5)

    def _work(self) -> None:
        while not self._stopped.is_set():
            self.process_next(timeout=0.25)

    def process_next(self, timeout: float = 0.0) -> bool:
        """Run one queued job; False when none arrived within ``timeout`` seconds."""
        if self.state is State.IDLE and self._deferred:
            job: _Job | None = self._deferred.pop(0)
        else:
            try:
                job = self._jobs.get(timeout=timeout) if timeout > 0 else self._jobs.get_nowait()
            except queue.Empty:
                return False
        if job is None:
            return False
        try:
            self._run(job)
        except Exception as exc:  # last resort: never let the worker die
            log.exception("voice loop job failed")
            self._fail(exc)
        return True

    def _run(self, job: _Job) -> None:
        if job.kind == "turn":
            self._listen_loop()
        elif job.kind == "text":
            self._message("you", job.text)
            self._respond(job.text)
            self._listen_loop()
        else:
            if self.state is not State.IDLE:
                self._deferred.append(job)
                return
            self._message(job.label, job.text)
            self._speak(job.text, detail=job.label, chime=self.chime)
            self._listen_loop()

    def _listen_loop(self) -> None:
        """Runs turns while LISTENING; a barge-in re-enters LISTENING."""
        while self.state is State.LISTENING and not self._stopped.is_set():
            if self._recorder is None or self._transcriber is None:
                self._transition(State.IDLE, "no voice input")
                return
            try:
                utterance = self._recorder.record()
            except Exception as exc:
                self._fail(exc)
                return
            if utterance is None:
                self._message("info", "No speech heard.")
                self._transition(State.IDLE, "timeout")
                return
            self._transition(State.THINKING, "transcribing")
            try:
                text = self._transcriber.transcribe(utterance.samples, utterance.sample_rate)
            except Exception as exc:
                self._fail(exc)
                return
            text = text.strip()
            if not text:
                self._message("info", "Sorry, I didn't catch that.")
                self._transition(State.IDLE, "nothing transcribed")
                return
            self._message("you", text)
            self._respond(text)

    def _respond(self, text: str) -> None:
        self._notify(State.THINKING, "thinking")
        try:
            reply = self._ask(text)
        except Exception as exc:
            self._fail(exc)
            return
        self._message("assistant", reply)
        self._speak(reply, detail="replying")

    def _speak(self, text: str, *, detail: str, chime: bool = False) -> None:
        self._transition(State.SPEAKING, detail)
        try:
            self._speaker.speak(text, chime=chime)
        except Exception as exc:
            self._fail(exc, speak=False)
            return
        with self._lock:
            finished = self._state is State.SPEAKING
        if finished:  # otherwise a barge-in already moved us to LISTENING
            self._transition(State.IDLE, "done")

    def _fail(self, exc: BaseException, *, speak: bool = True) -> None:
        log.warning("voice loop error: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
        message = self._error_text(exc)
        self._message("error", message)
        self._transition(State.IDLE, "error")
        if not speak:
            return
        self._transition(State.SPEAKING, "error")
        try:
            self._speaker.speak(f"Sorry. {message}")
        except Exception as speak_exc:
            log.warning("could not speak the error: %s", speak_exc)
        self._transition(State.IDLE, "error")


# -- wake word detection --------------------------------------------------


class WakeDetector(Protocol):
    def process(self, frame: Int16Array) -> bool: ...

    def reset(self) -> None: ...


def phrase_to_model(phrase: str) -> str:
    """``"Hey Jarvis"`` to openWakeWord's ``hey_jarvis``; model paths pass through."""
    if phrase.endswith((".onnx", ".tflite")):
        return str(Path(phrase).expanduser())
    return re.sub(r"[^a-z0-9]+", "_", phrase.strip().lower()).strip("_")


class OpenWakeWordDetector:
    """openWakeWord on 80 ms frames of 16 kHz int16 audio, loaded on first use."""

    def __init__(
        self,
        phrase: str = "hey jarvis",
        *,
        threshold: float = 0.5,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.model_name = phrase_to_model(phrase)
        self.threshold = threshold
        self._factory = model_factory
        self._model: Any = None

    def _default_factory(self, name: str) -> Any:
        try:
            from openwakeword import utils as oww_utils
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError("wake word needs the 'voice' extra (openwakeword)") from exc
        is_path = name.endswith((".onnx", ".tflite"))
        framework = "tflite" if name.endswith(".tflite") else "onnx"
        if not is_path:
            # Fetches the pretrained models on first run (cached afterwards).
            oww_utils.download_models(model_names=[name])
        return Model(wakeword_models=[name], inference_framework=framework)

    def load(self) -> Any:
        if self._model is None:
            factory = self._factory or self._default_factory
            self._model = factory(self.model_name)
        return self._model

    def process(self, frame: Int16Array) -> bool:
        scores = self.load().predict(frame)
        return bool(scores) and max(float(s) for s in scores.values()) >= self.threshold

    def reset(self) -> None:
        if self._model is not None:
            self._model.reset()


class FrameSource(Protocol):
    def read(self, timeout: float | None = None) -> Int16Array | None: ...


class AudioRouter:
    """Sends microphone frames to the wake detector (IDLE) or the recorder (LISTENING).

    While idle it also teaches ``noise`` the room's ambient level, which the
    end-of-speech detector uses as its floor.
    """

    def __init__(
        self,
        source: FrameSource,
        handler: WakeWordHandler,
        listen_queue: queue.Queue[Int16Array],
        detector: WakeDetector | None = None,
        noise: NoiseFloor | None = None,
    ) -> None:
        self._source = source
        self._handler = handler
        self._listen_queue = listen_queue
        self._detector = detector
        self._noise = noise
        self._previous = State.IDLE
        self._stop = threading.Event()

    def pump(self, timeout: float | None = 0.5) -> None:
        frame = self._source.read(timeout)
        if frame is None:
            return
        state = self._handler.state
        if state is not self._previous:
            if state is State.IDLE and self._detector is not None:
                self._detector.reset()
            self._previous = state
        if state is State.LISTENING:
            self._listen_queue.put(frame)
            return
        if state is not State.IDLE:
            return  # the assistant's own voice is neither noise nor a wake word
        if self._noise is not None:
            self._noise.update(frame)
        if self._detector is not None:
            try:
                fired = self._detector.process(frame)
            except Exception:
                log.exception("wake word detection failed; disabling it")
                self._detector = None
                return
            if fired:
                self._detector.reset()
                self._handler.trigger(Trigger.WAKE_WORD)

    def run(self) -> None:
        while not self._stop.is_set():
            self.pump()

    def stop(self) -> None:
        self._stop.set()
