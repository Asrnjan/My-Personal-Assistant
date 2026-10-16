"""Speech-to-text: an energy end-of-speech detector and a faster-whisper wrapper.

The endpointer decides, frame by frame, when the CEO has finished speaking.
faster-whisper then transcribes the captured audio locally; its own Silero VAD
filter (``vad_filter``) trims silence and noise before decoding.
"""

from __future__ import annotations

import enum
import logging
import queue
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from assistant_desktop.audio import (
    MIC_SAMPLE_RATE,
    Float32Array,
    Int16Array,
    decode_wav,
    resample,
    rms_dbfs,
    to_float32,
)

log = logging.getLogger(__name__)

WHISPER_SAMPLE_RATE = 16_000


class SttUnavailableError(RuntimeError):
    """faster-whisper is not installed or the model cannot be loaded."""


class Endpoint(enum.Enum):
    SILENCE = "silence"  # speech followed by enough trailing silence
    MAX_DURATION = "max_duration"  # the utterance hit the length cap
    NO_SPEECH = "no_speech"  # nothing was said before the start timeout


class NoiseFloor:
    """Ambient level in dBFS, learnt from the microphone while the assistant is idle.

    The value is a low percentile of recent frame levels, so a cough or a
    passing conversation does not drag it up the way a mean would.
    """

    def __init__(
        self, *, window_frames: int = 40, percentile: float = 20.0, default_dbfs: float = -70.0
    ) -> None:
        self._levels: deque[float] = deque(maxlen=window_frames)
        self._percentile = percentile
        self._default = default_dbfs

    def update(self, frame: NDArray[Any]) -> None:
        self._levels.append(rms_dbfs(frame))

    @property
    def value(self) -> float:
        if not self._levels:
            return self._default
        return float(np.percentile(np.fromiter(self._levels, dtype=np.float64), self._percentile))


@dataclass
class EnergyEndpointer:
    """Streaming end-of-speech detection on RMS energy.

    Feed frames with :meth:`push`; it returns an :class:`Endpoint` once the
    utterance is over and ``None`` while it is still going. A frame is speech
    when it is louder than both ``threshold_dbfs`` and the room's noise floor
    plus ``noise_margin_db``, so a noisy room does not count as talking.
    Durations are counted in samples to avoid floating-point drift.
    """

    sample_rate: int = MIC_SAMPLE_RATE
    threshold_dbfs: float = -45.0
    silence_seconds: float = 0.8
    start_timeout_seconds: float = 6.0
    max_seconds: float = 15.0
    min_speech_seconds: float = 0.15
    noise_floor_dbfs: float = -70.0
    noise_margin_db: float = 10.0
    speech_started: bool = field(default=False, init=False)
    _elapsed: int = field(default=0, init=False)
    _voiced_run: int = field(default=0, init=False)
    _silence_run: int = field(default=0, init=False)

    def _samples(self, seconds: float) -> int:
        return round(seconds * self.sample_rate)

    @property
    def elapsed(self) -> float:
        return self._elapsed / self.sample_rate

    @property
    def effective_threshold(self) -> float:
        return max(self.threshold_dbfs, self.noise_floor_dbfs + self.noise_margin_db)

    def is_speech(self, frame: NDArray[Any]) -> bool:
        return rms_dbfs(frame) >= self.effective_threshold

    def push(self, frame: NDArray[Any]) -> Endpoint | None:
        count = len(frame)
        self._elapsed += count
        if self.is_speech(frame):
            self._voiced_run += count
            self._silence_run = 0
            if self._voiced_run >= self._samples(self.min_speech_seconds):
                self.speech_started = True
        else:
            self._voiced_run = 0
            self._silence_run += count
        if self.speech_started:
            if self._silence_run >= self._samples(self.silence_seconds):
                return Endpoint.SILENCE
            if self._elapsed >= self._samples(self.max_seconds):
                return Endpoint.MAX_DURATION
        elif self._elapsed >= self._samples(self.start_timeout_seconds):
            return Endpoint.NO_SPEECH
        return None


@dataclass(frozen=True)
class Utterance:
    samples: Int16Array
    sample_rate: int
    endpoint: Endpoint

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate


class QueueRecorder:
    """Collects one utterance from a frame queue filled by the audio router."""

    def __init__(
        self,
        frames: queue.Queue[Int16Array],
        endpointer_factory: Callable[[], EnergyEndpointer],
        *,
        sample_rate: int = MIC_SAMPLE_RATE,
        frame_timeout: float = 2.0,
    ) -> None:
        self._frames = frames
        self._factory = endpointer_factory
        self._sample_rate = sample_rate
        self._frame_timeout = frame_timeout

    def _drain(self) -> None:
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    def record(self) -> Utterance | None:
        """Block until end of speech; ``None`` when nobody spoke."""
        endpointer = self._factory()
        captured: list[Int16Array] = []
        try:
            while True:
                try:
                    frame = self._frames.get(timeout=self._frame_timeout)
                except queue.Empty:
                    raise RuntimeError("the microphone stopped delivering audio") from None
                captured.append(frame)
                result = endpointer.push(frame)
                if result is Endpoint.NO_SPEECH:
                    return None
                if result is not None:
                    return Utterance(np.concatenate(captured), self._sample_rate, result)
        finally:
            self._drain()


def prepare_audio(audio: bytes | NDArray[Any], sample_rate: int) -> Float32Array:
    """WAV bytes or a PCM array to 16 kHz mono float32, as Whisper expects."""
    if isinstance(audio, bytes | bytearray):
        samples, sample_rate = decode_wav(bytes(audio))
    else:
        samples = to_float32(audio)
    return resample(samples, sample_rate, WHISPER_SAMPLE_RATE)


class WhisperTranscriber:
    """faster-whisper ``WhisperModel``, loaded on first use."""

    def __init__(
        self,
        model_size: str = "small",
        *,
        compute_type: str = "int8",
        device: str = "cpu",
        language: str | None = "en",
        vad_filter: bool = True,
        beam_size: int = 1,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_size = model_size
        self.compute_type = compute_type
        self.device = device
        self.language = language
        self.vad_filter = vad_filter
        self.beam_size = beam_size
        self._factory = model_factory
        self._model: Any = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _default_factory(self) -> Callable[..., Any]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SttUnavailableError(
                "speech-to-text needs the 'voice' extra (faster-whisper)"
            ) from exc
        factory: Callable[..., Any] = WhisperModel
        return factory

    def load(self) -> Any:
        with self._lock:
            if self._model is None:
                factory = self._factory or self._default_factory()
                log.info(
                    "loading faster-whisper %s (%s on %s)",
                    self.model_size,
                    self.compute_type,
                    self.device,
                )
                self._model = factory(
                    self.model_size, device=self.device, compute_type=self.compute_type
                )
            return self._model

    def transcribe(
        self, audio: bytes | NDArray[Any], sample_rate: int = WHISPER_SAMPLE_RATE
    ) -> str:
        samples = prepare_audio(audio, sample_rate)
        if samples.size == 0:
            return ""
        model = self.load()
        segments, _info = model.transcribe(
            samples,
            language=self.language,
            vad_filter=self.vad_filter,
            beam_size=self.beam_size,
            condition_on_previous_text=False,
        )
        texts = [str(segment.text).strip() for segment in segments]
        return " ".join(t for t in texts if t)
