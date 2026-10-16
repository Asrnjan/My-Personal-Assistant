"""Audio helpers: WAV encode/decode, conversions, the chime, and device I/O.

Everything except :class:`SoundDevicePlayer` and :class:`MicrophoneStream` is
pure numpy. Those two import ``sounddevice`` (the ``voice`` extra) when first
used, so the rest of the client works without an audio stack.
"""

from __future__ import annotations

import io
import logging
import math
import queue
import threading
import wave
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

log = logging.getLogger(__name__)

Int16Array = NDArray[np.int16]
Float32Array = NDArray[np.float32]

MIC_SAMPLE_RATE = 16_000
MIC_FRAME_SAMPLES = 1_280  # 80 ms at 16 kHz, the frame size openWakeWord expects
_INT16_SCALE = 32_768.0


class AudioUnavailableError(RuntimeError):
    """sounddevice / PortAudio is missing or no device can be opened."""


@dataclass(frozen=True)
class AudioClip:
    """Mono 16-bit PCM."""

    samples: Int16Array
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate if self.sample_rate else 0.0

    def to_wav(self) -> bytes:
        return encode_wav(self.samples, self.sample_rate)


def to_float32(samples: NDArray[Any]) -> Float32Array:
    """Any int/float PCM array to float32 in [-1, 1], downmixing to mono."""
    arr = np.asarray(samples)
    if arr.dtype == np.int16:
        scaled = arr.astype(np.float64) / _INT16_SCALE
    elif arr.dtype == np.int32:
        scaled = arr.astype(np.float64) / 2_147_483_648.0
    elif arr.dtype == np.uint8:
        scaled = (arr.astype(np.float64) - 128.0) / 128.0
    elif arr.dtype.kind == "f":
        scaled = np.clip(arr.astype(np.float64), -1.0, 1.0)
    else:
        raise ValueError(f"unsupported sample type {arr.dtype}")
    if scaled.ndim == 2:
        scaled = scaled.mean(axis=1)
    elif scaled.ndim != 1:
        raise ValueError("audio must be a 1-D (mono) or 2-D (frames x channels) array")
    return scaled.astype(np.float32)


def to_int16(samples: NDArray[Any]) -> Int16Array:
    arr = np.asarray(samples)
    if arr.dtype == np.int16 and arr.ndim == 1:
        return arr
    floats = to_float32(arr)
    return np.round(floats * 32_767.0).astype(np.int16)


def resample(samples: Float32Array, source_rate: int, target_rate: int) -> Float32Array:
    """Linear-interpolation resampling; adequate for speech going into Whisper."""
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.float32)
    count = round(samples.size * target_rate / source_rate)
    positions = np.linspace(0.0, samples.size - 1, num=max(count, 1))
    out = np.interp(positions, np.arange(samples.size), samples)
    return out.astype(np.float32)


def encode_wav(samples: NDArray[Any], sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(to_int16(samples).tobytes())
    return buf.getvalue()


def decode_wav(data: bytes) -> tuple[Float32Array, int]:
    """WAV bytes to (mono float32 samples, sample rate)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"not a PCM WAV file: {exc}") from exc
    dtypes: dict[int, Any] = {1: np.uint8, 2: np.int16, 4: np.int32}
    if width not in dtypes:
        raise ValueError(f"unsupported WAV sample width: {width * 8} bits")
    raw = np.frombuffer(frames, dtype=dtypes[width])
    if channels > 1:
        raw = raw.reshape(-1, channels)
    return to_float32(raw), rate


def decode_wav_clip(data: bytes) -> AudioClip:
    samples, rate = decode_wav(data)
    return AudioClip(to_int16(samples), rate)


def rms_dbfs(frame: NDArray[Any]) -> float:
    """Loudness of a frame in dB relative to full scale (silence is -120)."""
    floats = to_float32(frame)
    if floats.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(floats, dtype=np.float64))))
    return max(20.0 * math.log10(rms), -120.0) if rms > 0 else -120.0


def tone(
    frequency: float, seconds: float, sample_rate: int = 22_050, amplitude: float = 0.5
) -> Float32Array:
    t = np.arange(round(seconds * sample_rate)) / sample_rate
    return (amplitude * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)


def silence(seconds: float, sample_rate: int) -> Float32Array:
    return np.zeros(round(seconds * sample_rate), dtype=np.float32)


def generate_chime(sample_rate: int = 22_050, volume: float = 0.35) -> AudioClip:
    """A soft two-note bell (A5 then E6) with a quiet octave shimmer.

    Synthesised at runtime, so no binary asset ships with the package.
    """
    notes = ((880.0, 0.0), (1318.51, 0.14))
    length = 0.9
    total = np.zeros(round(length * sample_rate), dtype=np.float64)
    for freq, start in notes:
        n = round((length - start) * sample_rate)
        t = np.arange(n) / sample_rate
        attack = np.minimum(t / 0.012, 1.0)
        envelope = attack * np.exp(-t * 5.5)
        voice = np.sin(2 * np.pi * freq * t) + 0.18 * np.sin(2 * np.pi * freq * 2 * t)
        offset = round(start * sample_rate)
        total[offset : offset + n] += voice * envelope
    peak = float(np.max(np.abs(total))) or 1.0
    return AudioClip(to_int16((total / peak * volume).astype(np.float32)), sample_rate)


def concat(clips: list[AudioClip], gap_seconds: float = 0.0) -> AudioClip:
    if not clips:
        raise ValueError("nothing to concatenate")
    rate = clips[0].sample_rate
    parts: list[Int16Array] = []
    for i, clip in enumerate(clips):
        floats = to_float32(clip.samples)
        if clip.sample_rate != rate:
            floats = resample(floats, clip.sample_rate, rate)
        if i and gap_seconds:
            parts.append(to_int16(silence(gap_seconds, rate)))
        parts.append(to_int16(floats))
    return AudioClip(np.concatenate(parts), rate)


def import_sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:  # OSError: PortAudio library missing
        raise AudioUnavailableError(
            "audio needs the 'voice' extra (sounddevice) and PortAudio"
        ) from exc
    return sounddevice


class SoundDevicePlayer:
    """Blocking playback that another thread can cut short with :meth:`stop`."""

    def __init__(self, device: int | str | None = None, blocksize: int = 1_024) -> None:
        self._device = device
        self._blocksize = blocksize
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def play(self, clip: AudioClip) -> bool:
        """Play ``clip``; returns False when stopped before the end."""
        sd = import_sounddevice()
        self._stop.clear()
        data = clip.samples.reshape(-1, 1)
        position = 0
        finished = threading.Event()
        interrupted = False

        def callback(outdata: Any, frames: int, time_info: Any, status: Any) -> None:
            nonlocal position, interrupted
            if self._stop.is_set():
                interrupted = True
                outdata.fill(0)
                raise sd.CallbackStop
            chunk = data[position : position + frames]
            outdata[: len(chunk)] = chunk
            outdata[len(chunk) :] = 0
            position += frames
            if position >= len(data):
                raise sd.CallbackStop

        with sd.OutputStream(
            samplerate=clip.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=callback,
            finished_callback=finished.set,
        ):
            while not finished.wait(0.05):
                if self._stop.is_set():
                    interrupted = True
                    break
        return not interrupted


class MicrophoneStream:
    """16 kHz mono int16 frames from the default input, via a bounded queue."""

    def __init__(
        self,
        sample_rate: int = MIC_SAMPLE_RATE,
        frame_samples: int = MIC_FRAME_SAMPLES,
        device: int | str | None = None,
        max_frames: int = 200,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._device = device
        self._frames: queue.Queue[Int16Array] = queue.Queue(maxsize=max_frames)
        self._stream: Any = None

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            log.debug("microphone status: %s", status)
        frame = np.array(indata[:, 0], dtype=np.int16, copy=True)
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            # Drop the oldest frame: fresh audio matters more than stale audio.
            try:
                self._frames.get_nowait()
            except queue.Empty:
                log.debug("microphone queue drained concurrently")
            self._frames.put_nowait(frame)

    def start(self) -> None:
        sd = import_sounddevice()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self.frame_samples,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()

    def read(self, timeout: float | None = None) -> Int16Array | None:
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
