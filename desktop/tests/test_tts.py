from __future__ import annotations

import sys
import types
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from assistant_desktop.audio import AudioClip, decode_wav, generate_chime
from assistant_desktop.config import TtsSettings
from assistant_desktop.tts import (
    KOKORO_SAMPLE_RATE,
    KokoroSynthesizer,
    PiperSynthesizer,
    TextSpeaker,
    TtsUnavailableError,
    VoiceSpeaker,
    clean_for_speech,
    create_synthesizer,
    split_sentences,
)


def test_clean_for_speech() -> None:
    text = "## Today\n- **Board call** at 4\n1. Read [the memo](https://x)\n`code`"
    assert clean_for_speech(text) == "Today\nBoard call at 4\nRead the memo\ncode"


def test_split_sentences() -> None:
    assert split_sentences("Hi there.  Two items: one; two!\nDone?") == [
        "Hi there.",
        "Two items:",
        "one;",
        "two!",
        "Done?",
    ]
    assert split_sentences("   ") == []


# -- Piper ------------------------------------------------------------------


class FakePiperVoice13:
    """piper-tts >= 1.3: ``synthesize_wav`` writes a complete WAV."""

    def __init__(self, fixture: bytes) -> None:
        self.samples, self.rate = decode_wav(fixture)
        self.texts: list[str] = []
        self.configs: list[Any] = []

    def synthesize_wav(self, text: str, wav_file: wave.Wave_write, syn_config: Any = None) -> None:
        self.texts.append(text)
        self.configs.append(syn_config)
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(self.rate)
        wav_file.writeframes((self.samples * 32767).astype(np.int16).tobytes())


class FakePiperVoice12:
    """piper-tts 1.2: ``synthesize(text, wav_file, length_scale=...)``."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def synthesize(self, text: str, wav_file: wave.Wave_write, **kwargs: Any) -> None:
        self.kwargs = kwargs
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22_050)
        wav_file.writeframes(b"\x01\x00" * 2205)


def test_piper_synthesizes_wav(tmp_path: Path, tone_wav: bytes) -> None:
    voice = FakePiperVoice13(tone_wav)
    loaded: list[Path] = []

    def loader(path: Path) -> FakePiperVoice13:
        loaded.append(path)
        return voice

    synth = PiperSynthesizer("en_US-lessac-medium", tmp_path, voice_loader=loader)
    clip = synth.synthesize("Good morning.")
    assert clip.sample_rate == 16_000
    assert clip.samples.dtype == np.int16
    assert clip.duration == pytest.approx(0.5)
    assert voice.texts == ["Good morning."]
    assert loaded == [tmp_path / "en_US-lessac-medium.onnx"]
    synth.synthesize("Again.")
    assert len(loaded) == 1  # loaded once
    assert voice.configs == [None, None]
    assert decode_wav(clip.to_wav())[1] == 16_000


def test_piper_speed_uses_synthesis_config(
    tmp_path: Path, tone_wav: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SynthesisConfig:
        def __init__(self, length_scale: float) -> None:
            self.length_scale = length_scale

    monkeypatch.setitem(
        sys.modules, "piper", types.SimpleNamespace(SynthesisConfig=SynthesisConfig)
    )
    voice = FakePiperVoice13(tone_wav)
    synth = PiperSynthesizer("v", tmp_path, length_scale=0.8, voice_loader=lambda _p: voice)
    synth.synthesize("Faster.")
    assert voice.configs[0].length_scale == 0.8


def test_piper_legacy_api(tmp_path: Path) -> None:
    voice = FakePiperVoice12()
    synth = PiperSynthesizer(
        str(tmp_path / "custom.onnx"), tmp_path, length_scale=0.8, voice_loader=lambda _p: voice
    )
    assert synth.model_path() == tmp_path / "custom.onnx"
    clip = synth.synthesize("Hello")
    assert clip.sample_rate == 22_050
    assert len(clip.samples) == 2205
    assert voice.kwargs == {"length_scale": 0.8}


def test_piper_missing_voice_file(tmp_path: Path) -> None:
    with pytest.raises(TtsUnavailableError, match="download_voices"):
        PiperSynthesizer("en_US-lessac-medium", tmp_path).synthesize("hi")


def test_piper_missing_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "v.onnx").write_bytes(b"onnx")
    monkeypatch.setitem(sys.modules, "piper", None)
    with pytest.raises(TtsUnavailableError, match="piper-tts"):
        PiperSynthesizer("v", tmp_path).synthesize("hi")


def test_piper_default_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "v.onnx").write_bytes(b"onnx")
    voice = FakePiperVoice12()
    loaded: list[str] = []

    class PiperVoice:
        @staticmethod
        def load(path: str) -> FakePiperVoice12:
            loaded.append(path)
            return voice

    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=PiperVoice))
    PiperSynthesizer("v", tmp_path).synthesize("hi")
    assert loaded == [str(tmp_path / "v.onnx")]


# -- Kokoro -----------------------------------------------------------------


class FakeTensor:
    def __init__(self, data: np.ndarray[Any, Any]) -> None:
        self.data = data

    def detach(self) -> FakeTensor:
        return self

    def cpu(self) -> FakeTensor:
        return self

    def numpy(self) -> np.ndarray[Any, Any]:
        return self.data


class FakeResult:
    def __init__(self, audio: Any) -> None:
        self.audio = audio


class FakeKPipeline:
    def __init__(self, lang_code: str) -> None:
        self.lang_code = lang_code
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, text: str, **kwargs: Any) -> Any:
        self.calls.append((text, kwargs))
        chunk = np.full(2400, 0.5, dtype=np.float32)
        yield ("gs", "ps", chunk)
        yield ("gs", "ps", None)
        yield FakeResult(FakeTensor(chunk))


def test_kokoro_synthesizes_chunks() -> None:
    pipelines: list[FakeKPipeline] = []

    def factory(lang: str) -> FakeKPipeline:
        pipelines.append(FakeKPipeline(lang))
        return pipelines[-1]

    synth = KokoroSynthesizer("af_bella", lang_code="b", speed=1.2, pipeline_factory=factory)
    clip = synth.synthesize("Hello there")
    assert clip.sample_rate == KOKORO_SAMPLE_RATE
    assert len(clip.samples) == 4800
    assert int(clip.samples[0]) == pytest.approx(16_384, abs=2)
    assert pipelines[0].lang_code == "b"
    assert pipelines[0].calls == [("Hello there", {"voice": "af_bella", "speed": 1.2})]
    synth.synthesize("again")
    assert len(pipelines) == 1


def test_kokoro_empty_output() -> None:
    class Empty:
        def __call__(self, text: str, **kwargs: Any) -> Any:
            return iter(())

    clip = KokoroSynthesizer(pipeline_factory=lambda _l: Empty()).synthesize("x")
    assert clip.samples.size == 0


def test_kokoro_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "kokoro", None)
    with pytest.raises(TtsUnavailableError, match="kokoro"):
        KokoroSynthesizer().synthesize("hi")


def test_kokoro_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=FakeKPipeline))
    pipeline = KokoroSynthesizer(lang_code="a").load()
    assert isinstance(pipeline, FakeKPipeline)
    assert pipeline.lang_code == "a"


def test_create_synthesizer() -> None:
    piper = create_synthesizer(TtsSettings(speed=2.0))
    assert isinstance(piper, PiperSynthesizer)
    assert piper.voice == "en_US-lessac-medium"
    assert piper.length_scale == 0.5
    kokoro = create_synthesizer(TtsSettings(engine="kokoro", kokoro_lang="b"))
    assert isinstance(kokoro, KokoroSynthesizer)
    assert kokoro.voice == "af_heart"
    assert kokoro.lang_code == "b"


# -- speakers ---------------------------------------------------------------


class RecordingSynth:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str) -> AudioClip:
        self.texts.append(text)
        if text == "Silent.":
            return AudioClip(np.zeros(0, dtype=np.int16), 16_000)
        return AudioClip(np.ones(160, dtype=np.int16), 16_000)


class RecordingPlayer:
    def __init__(self, interrupt_on: int | None = None) -> None:
        self.played: list[AudioClip] = []
        self.stops = 0
        self.interrupt_on = interrupt_on
        self.speaker: VoiceSpeaker | None = None

    def play(self, clip: AudioClip) -> bool:
        self.played.append(clip)
        if self.interrupt_on is not None and len(self.played) == self.interrupt_on:
            assert self.speaker is not None
            self.speaker.stop()
            return False
        return True

    def stop(self) -> None:
        self.stops += 1


def test_voice_speaker_speaks_sentences_with_chime() -> None:
    synth, player = RecordingSynth(), RecordingPlayer()
    chime = generate_chime()
    speaker = VoiceSpeaker(synth, player, chime=chime)
    assert speaker.speak("**Good morning.** Silent. Three items today.", chime=True) is True
    assert synth.texts == ["Good morning.", "Silent.", "Three items today."]
    assert player.played[0] is chime
    assert len(player.played) == 3  # chime + two non-empty sentences


def test_voice_speaker_without_chime_clip() -> None:
    synth, player = RecordingSynth(), RecordingPlayer()
    assert VoiceSpeaker(synth, player).speak("Hi.", chime=True) is True
    assert len(player.played) == 1


def test_voice_speaker_barge_in_stops_remaining_sentences() -> None:
    synth, player = RecordingSynth(), RecordingPlayer(interrupt_on=1)
    speaker = VoiceSpeaker(synth, player)
    player.speaker = speaker
    assert speaker.speak("One. Two. Three.") is False
    assert synth.texts == ["One."]
    assert player.stops == 1


def test_voice_speaker_interrupted_during_chime() -> None:
    synth, player = RecordingSynth(), RecordingPlayer(interrupt_on=1)
    speaker = VoiceSpeaker(synth, player, chime=generate_chime())
    player.speaker = speaker
    assert speaker.speak("One.", chime=True) is False
    assert synth.texts == []


def test_voice_speaker_stop_during_synthesis() -> None:
    player = RecordingPlayer()

    class StoppingSynth(RecordingSynth):
        def synthesize(self, text: str) -> AudioClip:
            clip = super().synthesize(text)
            speaker.stop()
            return clip

    synth = StoppingSynth()
    speaker = VoiceSpeaker(synth, player)
    assert speaker.speak("One. Two.") is False
    assert player.played == []


def test_text_speaker() -> None:
    shown: list[str] = []
    speaker = TextSpeaker(shown.append)
    assert speaker.speak("hello", chime=True) is True
    speaker.stop()
    assert shown == ["hello"]
    assert TextSpeaker().speak("quiet") is True
