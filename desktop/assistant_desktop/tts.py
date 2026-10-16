"""Text-to-speech: Piper (default) or Kokoro behind one interface, plus speakers.

A *synthesizer* turns text into an :class:`AudioClip`. A *speaker* is what the
voice loop talks to: it synthesizes sentence by sentence and plays each one,
so speech starts quickly and a barge-in stops it between or within sentences.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from assistant_desktop.audio import AudioClip, decode_wav_clip, to_float32, to_int16
from assistant_desktop.config import TtsSettings

log = logging.getLogger(__name__)

KOKORO_SAMPLE_RATE = 24_000


class TtsUnavailableError(RuntimeError):
    """The TTS engine is not installed or its voice model is missing."""


class Synthesizer(Protocol):
    def synthesize(self, text: str) -> AudioClip: ...


class Player(Protocol):
    def play(self, clip: AudioClip) -> bool: ...

    def stop(self) -> None: ...


_MARKDOWN = re.compile(r"(\*\*|__|`+|^#+\s*|^\s*[-*•]\s+|^\s*\d+[.)]\s+)", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\n+")


def clean_for_speech(text: str) -> str:
    """Strip markdown so the voice does not read out asterisks and hashes."""
    text = _LINK.sub(r"\1", text)
    text = _MARKDOWN.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(text) if part and part.strip()]


class PiperSynthesizer:
    """Piper voice (``<voice>.onnx`` + ``.onnx.json``), loaded on first use.

    ``voice`` is a voice name looked up in ``model_dir`` or a path to an
    ``.onnx`` file. Download voices with
    ``python -m piper.download_voices en_US-lessac-medium``.
    """

    def __init__(
        self,
        voice: str,
        model_dir: Path,
        *,
        length_scale: float = 1.0,
        voice_loader: Callable[[Path], Any] | None = None,
    ) -> None:
        self.voice = voice
        self.model_dir = model_dir
        self.length_scale = length_scale
        self._loader = voice_loader
        self._voice: Any = None
        self._lock = threading.Lock()

    def model_path(self) -> Path:
        candidate = Path(self.voice).expanduser()
        if candidate.suffix == ".onnx":
            return candidate
        return self.model_dir / f"{self.voice}.onnx"

    def _default_loader(self) -> Callable[[Path], Any]:
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise TtsUnavailableError("Piper needs the 'voice' extra (piper-tts)") from exc

        def load(path: Path) -> Any:
            return PiperVoice.load(str(path))

        return load

    def load(self) -> Any:
        with self._lock:
            if self._voice is None:
                path = self.model_path()
                if self._loader is None and not path.is_file():
                    raise TtsUnavailableError(
                        f"Piper voice not found at {path}; download it with "
                        f"'python -m piper.download_voices --download-dir {self.model_dir} "
                        f"{self.voice}'"
                    )
                loader = self._loader or self._default_loader()
                self._voice = loader(path)
            return self._voice

    def _synthesis_config(self) -> Any:
        if self.length_scale == 1.0:
            return None
        from piper import SynthesisConfig

        return SynthesisConfig(length_scale=self.length_scale)

    def synthesize(self, text: str) -> AudioClip:
        voice = self.load()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            if hasattr(voice, "synthesize_wav"):  # piper-tts >= 1.3
                voice.synthesize_wav(text, wav_file, syn_config=self._synthesis_config())
            else:  # piper-tts 1.2
                voice.synthesize(text, wav_file, length_scale=self.length_scale)
        return decode_wav_clip(buf.getvalue())


class KokoroSynthesizer:
    """Kokoro-82M through ``kokoro.KPipeline`` (24 kHz), loaded on first use."""

    def __init__(
        self,
        voice: str = "af_heart",
        *,
        lang_code: str = "a",
        speed: float = 1.0,
        pipeline_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.voice = voice
        self.lang_code = lang_code
        self.speed = speed
        self._factory = pipeline_factory
        self._pipeline: Any = None
        self._lock = threading.Lock()

    def load(self) -> Any:
        with self._lock:
            if self._pipeline is None:
                factory = self._factory
                if factory is None:
                    try:
                        from kokoro import KPipeline
                    except ImportError as exc:
                        raise TtsUnavailableError(
                            "Kokoro needs the 'kokoro' extra (pip install kokoro)"
                        ) from exc

                    def factory(lang: str) -> Any:
                        return KPipeline(lang_code=lang)

                self._pipeline = factory(self.lang_code)
            return self._pipeline

    def synthesize(self, text: str) -> AudioClip:
        pipeline = self.load()
        parts: list[np.ndarray[Any, Any]] = []
        for result in pipeline(text, voice=self.voice, speed=self.speed):
            audio = result[2] if isinstance(result, tuple) else result.audio
            if audio is None:
                continue
            if hasattr(audio, "detach"):  # torch tensor
                audio = audio.detach().cpu().numpy()
            parts.append(to_float32(np.asarray(audio, dtype=np.float32)))
        samples = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return AudioClip(to_int16(samples), KOKORO_SAMPLE_RATE)


def create_synthesizer(settings: TtsSettings) -> Synthesizer:
    if settings.engine == "kokoro":
        return KokoroSynthesizer(
            settings.effective_voice, lang_code=settings.kokoro_lang, speed=settings.speed
        )
    return PiperSynthesizer(
        settings.effective_voice, settings.model_dir, length_scale=1.0 / settings.speed
    )


class VoiceSpeaker:
    """Speaks through a synthesizer and a player; :meth:`stop` cuts it short."""

    def __init__(
        self,
        synthesizer: Synthesizer,
        player: Player,
        *,
        chime: AudioClip | None = None,
    ) -> None:
        self._synth = synthesizer
        self._player = player
        self._chime = chime
        self._stopped = threading.Event()

    def stop(self) -> None:
        self._stopped.set()
        self._player.stop()

    def speak(self, text: str, *, chime: bool = False) -> bool:
        """Returns False when interrupted."""
        self._stopped.clear()
        if chime and self._chime is not None and not self._player.play(self._chime):
            return False
        for sentence in split_sentences(clean_for_speech(text)):
            if self._stopped.is_set():
                return False
            clip = self._synth.synthesize(sentence)
            if self._stopped.is_set():
                return False
            if clip.samples.size and not self._player.play(clip):
                return False
        return not self._stopped.is_set()


class TextSpeaker:
    """Used when no audio output is available: the text is shown instead."""

    def __init__(self, show: Callable[[str], None] | None = None) -> None:
        self._show = show

    def stop(self) -> None:
        """Nothing is playing, so there is nothing to stop."""

    def speak(self, text: str, *, chime: bool = False) -> bool:
        if self._show is not None:
            self._show(text)
        return True
