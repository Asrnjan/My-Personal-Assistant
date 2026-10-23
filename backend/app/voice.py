"""Server-side speech: faster-whisper for speech-to-text, Piper (or Kokoro) for
text-to-speech. Both run locally and are optional: install the ``voice`` extra.

Used by ``POST /stt`` and ``GET /tts`` (the PWA's fallback when the browser has
no speech engine) and by the Telegram voice-note loop.
"""

from __future__ import annotations

import importlib
import io
import logging
import shutil
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any, Protocol

from app.config import SttConfig, TtsConfig

log = logging.getLogger(__name__)

PIPER_SAMPLE_RATE = 22050
KOKORO_SAMPLE_RATE = 24000


class VoiceUnavailableError(RuntimeError):
    """The speech engine isn't installed or its model can't be loaded."""


class SpeechToText(Protocol):
    def transcribe(self, audio: bytes) -> str: ...


class TextToSpeech(Protocol):
    def synthesize(self, text: str) -> bytes: ...


def _import(module: str, extra: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise VoiceUnavailableError(
            f'{module} is not installed; run `pip install -e ".[{extra}]"` in backend/'
        ) from exc


def pcm16_wav(samples: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples)
    return buf.getvalue()


def float_to_pcm16(values: Any) -> bytes:
    """Convert a sequence of floats in [-1, 1] to little-endian 16-bit PCM."""
    out = bytearray()
    for v in values:
        clipped = max(-1.0, min(1.0, float(v)))
        out += int(clipped * 32767).to_bytes(2, "little", signed=True)
    return bytes(out)


class WhisperSTT:
    def __init__(self, config: SttConfig) -> None:
        self._config = config
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self, *, local_only: bool = False) -> Any:
        with self._lock:
            if self._model is None:
                module = _import("faster_whisper", "voice")
                kwargs: dict[str, Any] = {
                    "device": "cpu",
                    "compute_type": self._config.compute_type,
                }
                if local_only:
                    kwargs["local_files_only"] = True
                self._model = module.WhisperModel(self._config.model_size, **kwargs)
            return self._model

    def warm(self) -> threading.Thread:
        """Load the model in the background when it is already downloaded, so the first
        voice message after a restart isn't slow. Never downloads; failures are logged."""

        def run() -> None:
            try:
                self._load(local_only=True)
                log.info("speech model %s loaded", self._config.model_size)
            except Exception as exc:  # not installed, not downloaded yet, ...
                log.info("speech model not preloaded: %s", exc)

        thread = threading.Thread(target=run, name="stt-warm", daemon=True)
        thread.start()
        return thread

    def transcribe(self, audio: bytes) -> str:
        if not audio:
            return ""
        model = self._load()
        # Greedy decoding (beam_size=1) is several times faster on a CPU and just as good
        # for short spoken requests.
        segments, _info = model.transcribe(
            io.BytesIO(audio), language=self._config.language, vad_filter=True, beam_size=1
        )
        return " ".join(s.text.strip() for s in segments).strip()


class PiperTTS:
    def __init__(self, config: TtsConfig, voices_dir: Path) -> None:
        self._config = config
        self._voices_dir = voices_dir
        self._voice: Any = None
        self._lock = threading.Lock()

    def model_path(self) -> Path:
        voice = self._config.voice
        candidate = Path(voice).expanduser()
        if candidate.suffix == ".onnx":
            return candidate
        return self._voices_dir.expanduser() / f"{voice}.onnx"

    def _load(self) -> Any:
        with self._lock:
            if self._voice is None:
                module = _import("piper", "voice")
                path = self.model_path()
                if not path.exists():
                    raise VoiceUnavailableError(
                        f"Piper voice not found at {path}; see docs/SETUP.md (voices)"
                    )
                self._voice = module.PiperVoice.load(str(path))
            return self._voice

    def synthesize(self, text: str) -> bytes:
        voice = self._load()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w)
        return buf.getvalue()


class KokoroTTS:
    def __init__(self, config: TtsConfig) -> None:
        self._config = config
        self._pipeline: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._pipeline is None:
                module = _import("kokoro", "kokoro")
                self._pipeline = module.KPipeline(lang_code="a")
            return self._pipeline

    def synthesize(self, text: str) -> bytes:
        pipeline = self._load()
        pcm = bytearray()
        for _graphemes, _phonemes, audio in pipeline(text, voice=self._config.voice):
            values = audio.tolist() if hasattr(audio, "tolist") else audio
            pcm += float_to_pcm16(values)
        return pcm16_wav(bytes(pcm), KOKORO_SAMPLE_RATE)


def build_tts(config: TtsConfig, voices_dir: Path) -> TextToSpeech:
    if config.engine == "kokoro":
        return KokoroTTS(config)
    return PiperTTS(config, voices_dir)


_FFMPEG_OGG_ARGS = ("-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-f", "ogg", "pipe:1")


def wav_to_ogg(wav: bytes, ffmpeg: str | None = None) -> bytes | None:
    """Telegram voice notes must be OGG/Opus; convert with ffmpeg when it's installed."""
    binary = ffmpeg or shutil.which("ffmpeg")
    if binary is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [binary, *_FFMPEG_OGG_ARGS],
            input=wav,
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffmpeg could not convert the voice reply: %s", exc)
        return None
    return result.stdout or None
