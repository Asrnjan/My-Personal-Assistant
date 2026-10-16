from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest

from assistant_desktop import audio
from assistant_desktop.audio import (
    AudioClip,
    AudioUnavailableError,
    MicrophoneStream,
    SoundDevicePlayer,
    concat,
    decode_wav,
    decode_wav_clip,
    encode_wav,
    generate_chime,
    resample,
    rms_dbfs,
    silence,
    to_float32,
    to_int16,
    tone,
)


def test_decode_fixture_mono(tone_wav: bytes) -> None:
    samples, rate = decode_wav(tone_wav)
    assert rate == 16_000
    assert samples.dtype == np.float32
    assert len(samples) == 8_000
    assert 0.45 < float(np.max(np.abs(samples))) <= 0.5


def test_decode_fixture_stereo_downmixes(stereo_wav: bytes) -> None:
    samples, rate = decode_wav(stereo_wav)
    assert rate == 22_050
    assert samples.ndim == 1
    assert len(samples) == 11_025


def test_decode_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a PCM WAV"):
        decode_wav(b"RIFF....not really")


def test_decode_rejects_24_bit() -> None:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(3)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00\x00" * 10)
    with pytest.raises(ValueError, match="24 bits"):
        decode_wav(buf.getvalue())


def test_encode_decode_roundtrip() -> None:
    original = tone(1000, 0.1, 16_000, 0.3)
    samples, rate = decode_wav(encode_wav(original, 16_000))
    assert rate == 16_000
    assert np.allclose(samples, original, atol=1e-3)


def test_clip_helpers(silence_wav: bytes) -> None:
    clip = decode_wav_clip(silence_wav)
    assert clip.duration == pytest.approx(0.5)
    assert decode_wav(clip.to_wav())[1] == 16_000
    assert AudioClip(np.zeros(0, dtype=np.int16), 0).duration == 0.0


@pytest.mark.parametrize(
    ("array", "expected"),
    [
        (np.array([16_384, -32_768], dtype=np.int16), [0.5, -1.0]),
        (np.array([1_073_741_824], dtype=np.int32), [0.5]),
        (np.array([192, 128], dtype=np.uint8), [0.5, 0.0]),
        (np.array([2.0, -0.25], dtype=np.float64), [1.0, -0.25]),
        (np.array([[0.5, -0.5], [1.0, 0.0]], dtype=np.float32), [0.0, 0.5]),
    ],
)
def test_to_float32(array: np.ndarray[Any, Any], expected: list[float]) -> None:
    assert np.allclose(to_float32(array), expected)


def test_to_float32_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        to_float32(np.array(["a"]))
    with pytest.raises(ValueError, match="mono"):
        to_float32(np.zeros((2, 2, 2), dtype=np.float32))


def test_to_int16_passthrough_and_conversion() -> None:
    ints = np.array([1, 2], dtype=np.int16)
    assert to_int16(ints) is ints
    assert to_int16(np.array([1.0, -1.0])).tolist() == [32_767, -32_767]


def test_resample() -> None:
    samples = tone(200, 0.5, 22_050)
    out = resample(samples, 22_050, 16_000)
    assert len(out) == 8_000
    assert resample(samples, 16_000, 16_000) is not None
    assert len(resample(np.zeros(0, dtype=np.float32), 8000, 16_000)) == 0
    with pytest.raises(ValueError):
        resample(samples, 0, 16_000)


def test_rms_dbfs(tone_wav: bytes, silence_wav: bytes) -> None:
    loud, _ = decode_wav(tone_wav)
    quiet, _ = decode_wav(silence_wav)
    assert rms_dbfs(loud) == pytest.approx(20 * np.log10(0.5 / np.sqrt(2)), abs=0.1)
    assert rms_dbfs(quiet) == -120.0
    assert rms_dbfs(np.zeros(0, dtype=np.float32)) == -120.0


def test_chime_is_gentle_and_short() -> None:
    chime = generate_chime(16_000, volume=0.3)
    assert chime.sample_rate == 16_000
    assert 0.5 < chime.duration < 1.5
    peak = int(np.max(np.abs(chime.samples)))
    assert 0.25 * 32_767 < peak <= 0.31 * 32_767
    # Fades out: the last 50 ms is much quieter than the start.
    assert rms_dbfs(chime.samples[-800:]) < rms_dbfs(chime.samples[:3200]) - 15


def test_concat_resamples_and_gaps() -> None:
    a = AudioClip(to_int16(tone(440, 0.1, 16_000)), 16_000)
    b = AudioClip(to_int16(tone(440, 0.1, 8_000)), 8_000)
    joined = concat([a, b], gap_seconds=0.05)
    assert joined.sample_rate == 16_000
    assert len(joined.samples) == 1600 + 800 + 1600
    assert len(silence(0.1, 1000)) == 100
    with pytest.raises(ValueError):
        concat([])


# -- device glue with a fake sounddevice module ---------------------------


class CallbackStop(Exception):  # noqa: N818 - mirrors sounddevice's name
    pass


class FakeOutputStream:
    """Runs the callback synchronously on enter, like a very fast sound card."""

    before_block: Callable[[int], None] | None = None
    instances: list[FakeOutputStream] = []  # noqa: RUF012

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.written: list[np.ndarray[Any, Any]] = []
        FakeOutputStream.instances.append(self)

    def __enter__(self) -> FakeOutputStream:
        callback: Callable[..., None] = self.kwargs["callback"]
        blocksize = self.kwargs["blocksize"]
        block = 0
        while True:
            block += 1
            if FakeOutputStream.before_block is not None:
                FakeOutputStream.before_block(block)
            out = np.full((blocksize, 1), 7, dtype=np.int16)
            self.written.append(out)
            try:
                callback(out, blocksize, None, None)
            except CallbackStop:
                break
        self.kwargs["finished_callback"]()
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeInputStream:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = self.stopped = self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_sd(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.SimpleNamespace]:
    module = types.SimpleNamespace(
        CallbackStop=CallbackStop, OutputStream=FakeOutputStream, InputStream=FakeInputStream
    )
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    FakeOutputStream.before_block = None
    FakeOutputStream.instances = []
    yield module


def test_import_sounddevice_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    with pytest.raises(AudioUnavailableError):
        audio.import_sounddevice()


def test_player_plays_whole_clip(fake_sd: types.SimpleNamespace) -> None:
    player = SoundDevicePlayer(blocksize=100)
    clip = AudioClip(np.arange(250, dtype=np.int16), 8000)
    assert player.play(clip) is True
    stream = FakeOutputStream.instances[0]
    played = np.concatenate([w[:, 0] for w in stream.written])
    assert played[:250].tolist() == list(range(250))
    assert not played[250:].any()  # padding is silence
    assert stream.kwargs["samplerate"] == 8000


def test_player_stop_interrupts(fake_sd: types.SimpleNamespace) -> None:
    player = SoundDevicePlayer(blocksize=100)

    def stop_on_second(block: int) -> None:
        if block == 2:
            player.stop()

    FakeOutputStream.before_block = stop_on_second
    assert player.play(AudioClip(np.ones(10_000, dtype=np.int16), 8000)) is False
    written = FakeOutputStream.instances[0].written
    assert len(written) == 2
    assert not written[1].any()  # the interrupted block is silenced


def test_microphone_stream(fake_sd: types.SimpleNamespace) -> None:
    mic = MicrophoneStream(frame_samples=4, max_frames=2)
    mic.start()
    stream = mic._stream
    assert stream.started
    callback = stream.kwargs["callback"]
    for value in (1, 2, 3):
        callback(
            np.full((4, 1), value, dtype=np.int16), 4, None, "overflow" if value == 3 else None
        )
    # Queue holds two frames; the oldest (1) was dropped.
    first = mic.read(timeout=0.1)
    second = mic.read(timeout=0.1)
    assert first is not None and second is not None
    assert first.tolist() == [2, 2, 2, 2]
    assert second.tolist() == [3, 3, 3, 3]
    assert mic.read(timeout=0.01) is None
    mic.close()
    assert stream.stopped and stream.closed
    mic.close()  # second close is a no-op
