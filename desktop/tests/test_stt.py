from __future__ import annotations

import queue
import sys
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from assistant_desktop.audio import Int16Array, decode_wav, to_int16, tone
from assistant_desktop.stt import (
    Endpoint,
    EnergyEndpointer,
    NoiseFloor,
    QueueRecorder,
    SttUnavailableError,
    Utterance,
    WhisperTranscriber,
    prepare_audio,
)

RATE = 16_000
FRAME = 1_280  # 80 ms


def speech_frame(amplitude: float = 0.3) -> Int16Array:
    return to_int16(tone(300, FRAME / RATE, RATE, amplitude))


def quiet_frame() -> Int16Array:
    return np.zeros(FRAME, dtype=np.int16)


def frames(n: int, kind: str) -> list[Int16Array]:
    return [speech_frame() if kind == "speech" else quiet_frame() for _ in range(n)]


# -- endpointer -------------------------------------------------------------


def run(endpointer: EnergyEndpointer, sequence: list[Int16Array]) -> tuple[Endpoint | None, int]:
    for i, frame in enumerate(sequence, start=1):
        result = endpointer.push(frame)
        if result is not None:
            return result, i
    return None, len(sequence)


def test_end_of_speech_after_trailing_silence() -> None:
    ep = EnergyEndpointer(silence_seconds=0.8)
    result, count = run(ep, frames(5, "quiet") + frames(10, "speech") + frames(20, "quiet"))
    assert result is Endpoint.SILENCE
    assert ep.speech_started
    assert count == 5 + 10 + 10  # 10 quiet frames = 0.8 s


def test_short_pause_does_not_end_utterance() -> None:
    ep = EnergyEndpointer(silence_seconds=0.8)
    seq = frames(5, "speech") + frames(5, "quiet") + frames(5, "speech") + frames(3, "quiet")
    assert run(ep, seq) == (None, len(seq))


def test_no_speech_before_start_timeout() -> None:
    ep = EnergyEndpointer(start_timeout_seconds=0.8)
    result, count = run(ep, frames(20, "quiet"))
    assert result is Endpoint.NO_SPEECH
    assert count == 10


def test_max_duration_caps_long_speech() -> None:
    ep = EnergyEndpointer(max_seconds=1.6)
    result, count = run(ep, frames(40, "speech"))
    assert result is Endpoint.MAX_DURATION
    assert count == 20


def test_click_is_not_speech() -> None:
    ep = EnergyEndpointer(min_speech_seconds=0.15, start_timeout_seconds=0.8)
    seq = [speech_frame(), *frames(3, "quiet"), speech_frame(), *frames(10, "quiet")]
    result, _ = run(ep, seq)
    assert result is Endpoint.NO_SPEECH


def test_noise_floor_raises_threshold_in_a_noisy_room() -> None:
    hum = to_int16(tone(60, FRAME / RATE, RATE, 0.01))  # about -43 dBFS of room noise
    floor = NoiseFloor(window_frames=10)
    assert floor.value == -70.0
    for _ in range(20):
        floor.update(hum)
    floor.update(speech_frame())  # one loud frame barely moves a low percentile
    assert floor.value == pytest.approx(-43.0, abs=0.5)
    ep = EnergyEndpointer(threshold_dbfs=-60.0, noise_floor_dbfs=floor.value)
    assert ep.effective_threshold == pytest.approx(-33.0, abs=0.5)
    assert not ep.is_speech(hum)
    assert ep.is_speech(speech_frame())
    quiet_room = EnergyEndpointer(threshold_dbfs=-60.0)
    assert quiet_room.is_speech(hum)


def test_elapsed_reports_seconds() -> None:
    ep = EnergyEndpointer()
    ep.push(quiet_frame())
    assert ep.elapsed == pytest.approx(0.08)


def test_fixture_wavs_classified(tone_wav: bytes, silence_wav: bytes) -> None:
    ep = EnergyEndpointer()
    assert ep.is_speech(decode_wav(tone_wav)[0])
    assert not ep.is_speech(decode_wav(silence_wav)[0])


# -- recorder ---------------------------------------------------------------


def make_recorder(q: queue.Queue[Int16Array], **kwargs: Any) -> QueueRecorder:
    return QueueRecorder(q, lambda: EnergyEndpointer(**kwargs), frame_timeout=0.05)


def test_recorder_returns_utterance_and_drains() -> None:
    q: queue.Queue[Int16Array] = queue.Queue()
    for f in frames(3, "quiet") + frames(6, "speech") + frames(12, "quiet"):
        q.put(f)
    utterance = make_recorder(q, silence_seconds=0.4).record()
    assert utterance is not None
    assert utterance.endpoint is Endpoint.SILENCE
    assert utterance.sample_rate == RATE
    assert len(utterance.samples) == (3 + 6 + 5) * FRAME
    assert utterance.duration == pytest.approx(14 * 0.08)
    assert q.empty()  # leftover frames are dropped


def test_recorder_none_without_speech() -> None:
    q: queue.Queue[Int16Array] = queue.Queue()
    for f in frames(20, "quiet"):
        q.put(f)
    assert make_recorder(q, start_timeout_seconds=0.4).record() is None


def test_recorder_raises_when_microphone_stalls() -> None:
    q: queue.Queue[Int16Array] = queue.Queue()
    q.put(quiet_frame())
    with pytest.raises(RuntimeError, match="microphone"):
        make_recorder(q).record()


# -- transcriber ------------------------------------------------------------


@dataclass
class Segment:
    text: str


class FakeWhisperModel:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.init = (args, kwargs)
        self.calls: list[tuple[np.ndarray[Any, Any], dict[str, Any]]] = []

    def transcribe(self, audio: np.ndarray[Any, Any], **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append((audio, kwargs))
        segments = iter([Segment(" What's on "), Segment(""), Segment("my calendar? ")])
        return segments, {"language": "en"}


@pytest.fixture
def fake_model() -> FakeWhisperModel:
    return FakeWhisperModel()


def factory_for(model: FakeWhisperModel, created: list[tuple[Any, ...]]) -> Any:
    def factory(*args: Any, **kwargs: Any) -> FakeWhisperModel:
        created.append((args, kwargs))
        return model

    return factory


def test_transcribe_wav_bytes(tone_wav: bytes, fake_model: FakeWhisperModel) -> None:
    created: list[tuple[Any, ...]] = []
    stt = WhisperTranscriber(
        "small",
        compute_type="int8",
        language="en",
        model_factory=factory_for(fake_model, created),
    )
    assert not stt.loaded
    assert stt.transcribe(tone_wav) == "What's on my calendar?"
    assert stt.loaded
    assert created == [(("small",), {"device": "cpu", "compute_type": "int8"})]
    audio, kwargs = fake_model.calls[0]
    assert audio.dtype == np.float32 and len(audio) == 8_000
    assert kwargs["vad_filter"] is True
    assert kwargs["language"] == "en"
    assert kwargs["beam_size"] == 1
    # The model loads once.
    stt.transcribe(tone_wav)
    assert len(created) == 1


def test_transcribe_resamples_arrays(stereo_wav: bytes, fake_model: FakeWhisperModel) -> None:
    stt = WhisperTranscriber(
        language=None, vad_filter=False, model_factory=lambda *a, **k: fake_model
    )
    samples, rate = decode_wav(stereo_wav)
    stt.transcribe(to_int16(samples), rate)
    audio, kwargs = fake_model.calls[0]
    assert len(audio) == 8_000  # 22.05 kHz -> 16 kHz
    assert kwargs["language"] is None
    assert kwargs["vad_filter"] is False


def test_transcribe_empty_audio_skips_model() -> None:
    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("model must not load for empty audio")

    stt = WhisperTranscriber(model_factory=explode)
    assert stt.transcribe(np.zeros(0, dtype=np.int16), 16_000) == ""


def test_transcribe_utterance(fake_model: FakeWhisperModel) -> None:
    utterance = Utterance(speech_frame(), RATE, Endpoint.SILENCE)
    stt = WhisperTranscriber(model_factory=lambda *a, **k: fake_model)
    assert stt.transcribe(utterance.samples, utterance.sample_rate)


def test_concurrent_load_creates_one_model(fake_model: FakeWhisperModel) -> None:
    created: list[tuple[Any, ...]] = []
    stt = WhisperTranscriber(model_factory=factory_for(fake_model, created))
    threads = [threading.Thread(target=stt.load) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(created) == 1


def test_missing_faster_whisper(monkeypatch: pytest.MonkeyPatch, tone_wav: bytes) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(SttUnavailableError, match="voice"):
        WhisperTranscriber().transcribe(tone_wav)


def test_default_factory_uses_whisper_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    module = types.SimpleNamespace(WhisperModel=FakeWhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    model = WhisperTranscriber("tiny", compute_type="float32").load()
    assert isinstance(model, FakeWhisperModel)
    assert model.init == (("tiny",), {"device": "cpu", "compute_type": "float32"})


def test_prepare_audio_accepts_bytearray(tone_wav: bytes) -> None:
    assert len(prepare_audio(bytearray(tone_wav), 0)) == 8_000
