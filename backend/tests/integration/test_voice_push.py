from __future__ import annotations

import io
import json
import shutil
import sys
import types
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, SttConfig, TtsConfig
from app.events import Event
from app.main import create_app
from app.push import WebPusher, notification, pywebpush_sender
from app.voice import (
    KokoroTTS,
    PiperTTS,
    VoiceUnavailableError,
    WhisperSTT,
    build_tts,
    float_to_pcm16,
    pcm16_wav,
    wav_to_ogg,
)
from tests.harness import Harness, build_harness
from tests.integration.test_telegram_bot import FakeSTT, FakeTTS

ENDPOINT = "https://push.example.com/sub/1"
SUB = {
    "endpoint": ENDPOINT,
    "expirationTime": None,
    "keys": {"p256dh": "BPkey", "auth": "authkey"},
}


class FakePush:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.status: dict[str, int] = {}
        self.raise_for: set[str] = set()

    def __call__(self, subscription: dict[str, Any], payload: str) -> int:
        endpoint = subscription["endpoint"]
        if endpoint in self.raise_for:
            raise RuntimeError("boom")
        self.sent.append((endpoint, json.loads(payload)))
        return self.status.get(endpoint, 201)


@pytest.fixture
def voiced(settings: Settings) -> Iterator[tuple[Harness, FakeSTT, FakeTTS, FakePush]]:
    stt, tts, push = FakeSTT("drop the vendor review"), FakeTTS(), FakePush()
    for h in build_harness(settings, {"stt": stt, "tts": tts, "push_sender": push}):
        yield h, stt, tts, push


# --- routes --------------------------------------------------------------------------


def test_stt_and_tts_routes(voiced: tuple[Harness, FakeSTT, FakeTTS, FakePush]) -> None:
    h, stt, tts, _push = voiced
    resp = h.client.post("/stt", files={"audio": ("speech.webm", b"WEBM", "audio/webm")})
    assert resp.json() == {"text": "drop the vendor review"} and stt.heard == [b"WEBM"]
    spoken = h.client.get("/tts", params={"text": "**Good** morning. - one"})
    assert spoken.headers["content-type"] == "audio/wav" and spoken.content == b"RIFF-WAV"
    assert tts.spoken == ["Good morning. - one"]
    assert h.client.get("/tts", params={"text": ""}).status_code == 422
    stt.fail = tts.fail = True
    assert h.client.post("/stt", files={"audio": ("a.webm", b"x")}).status_code == 503
    assert h.client.get("/tts", params={"text": "hi"}).status_code == 503


def test_stt_rejects_huge_uploads(
    voiced: tuple[Harness, FakeSTT, FakeTTS, FakePush], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import routes_voice

    monkeypatch.setattr(routes_voice, "MAX_AUDIO_BYTES", 4)
    h = voiced[0]
    assert h.client.post("/stt", files={"audio": ("a.webm", b"12345")}).status_code == 413


def test_push_subscribe_and_delivery(voiced: tuple[Harness, FakeSTT, FakeTTS, FakePush]) -> None:
    h, _stt, _tts, push = voiced
    pusher: WebPusher = h.app.state.pusher
    assert h.client.get("/push/vapid-public-key").json() == {"key": ""}
    assert h.client.post("/push/subscribe", json=SUB).status_code == 201
    assert h.client.post("/push/subscribe", json=SUB).status_code == 201  # idempotent
    bad = {**SUB, "endpoint": "http://insecure.example.com"}
    assert h.client.post("/push/subscribe", json=bad).status_code == 422
    assert pusher.count() == 1

    h.app.state.events.publish(Event("brief", {"kind": "morning", "text": "Good morning. " * 40}))
    endpoint, payload = push.sent[-1]
    assert endpoint == ENDPOINT
    assert payload["title"] == "Your morning brief" and payload["url"] == "/?autostart=brief"
    assert len(payload["body"]) <= 240 and payload["body"].endswith("…")
    h.app.state.events.publish(Event("notice", {"text": "Rahul accepted."}))
    assert push.sent[-1][1] == {
        "title": "Chief of Staff",
        "body": "Rahul accepted.",
        "tag": "notice",
        "url": "/",
    }

    assert h.client.post("/push/unsubscribe", json={"endpoint": ENDPOINT}).status_code == 204
    assert h.client.post("/push/unsubscribe", json={"endpoint": ENDPOINT}).status_code == 404


def test_expired_and_failing_subscriptions(
    voiced: tuple[Harness, FakeSTT, FakeTTS, FakePush],
) -> None:
    h, _stt, _tts, push = voiced
    pusher: WebPusher = h.app.state.pusher
    for n in range(4):
        pusher.subscribe(f"https://push.example.com/{n}", "k", "a", None)
    push.status = {"https://push.example.com/0": 410, "https://push.example.com/1": 500}
    push.raise_for = {"https://push.example.com/2"}
    assert pusher.push({"title": "t", "body": "b"}) == 1
    assert pusher.count() == 3  # only the 410 was dropped


def test_notification_shapes() -> None:
    assert notification(Event("brief", {"kind": "alert", "text": "New P1: board"})) == {
        "title": "New P1",
        "body": "New P1: board",
        "tag": "alert",
        "url": "/",
    }
    assert notification(Event("brief", {"kind": "evening", "text": " "})) is None
    assert notification(Event("other", {"text": "x"})) is None


def test_push_unconfigured_is_503(settings: Settings) -> None:
    for h in build_harness(settings):
        assert h.app.state.pusher is None
        assert h.client.get("/push/vapid-public-key").status_code == 503
        assert h.client.post("/push/subscribe", json=SUB).status_code == 503


def test_pywebpush_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    import pywebpush

    calls: list[dict[str, Any]] = []

    def fake_webpush(subscription: dict[str, Any], data: str, **kwargs: Any) -> None:
        calls.append(kwargs)
        if subscription["endpoint"] == "gone":
            response = types.SimpleNamespace(status_code=410)
            raise pywebpush.WebPushException("gone", response=response)

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    send = pywebpush_sender("PRIVATE", "mailto:me@example.com")
    assert send({"endpoint": "ok"}, "{}") == 201
    assert send({"endpoint": "gone"}, "{}") == 410
    assert calls[0]["vapid_private_key"] == "PRIVATE"
    assert calls[0]["vapid_claims"] == {"sub": "mailto:me@example.com"}


def test_vapid_keys_enable_push(settings: Settings) -> None:
    from pydantic import SecretStr

    configured = settings.model_copy(
        update={"vapid_public_key": "BPub", "vapid_private_key": SecretStr("priv")}
    )
    app = create_app(configured)
    assert isinstance(app.state.pusher, WebPusher)
    app.state.engine.dispose()


# --- the PWA is served by the backend ----------------------------------------------------


def test_pwa_is_served_at_root(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "<html" in page.text.lower()
    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert client.get("/health").json()["status"] in {"ok", "degraded"}


def test_pwa_can_be_disabled(settings: Settings, tmp_path: Path) -> None:
    app = create_app(settings.model_copy(update={"pwa_dir": tmp_path}))
    with TestClient(app) as c:
        assert c.get("/").status_code == 404
    app.state.engine.dispose()


# --- speech engines (their libraries are replaced with fakes) ------------------------------


def test_wav_helpers() -> None:
    pcm = float_to_pcm16([0.0, 1.0, -1.0, 2.0])
    assert pcm == b"\x00\x00\xff\x7f\x01\x80\xff\x7f"
    with wave.open(io.BytesIO(pcm16_wav(pcm, 16000))) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 16000, 4)


def test_whisper_stt(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Model:
        def __init__(self, size: str, **kwargs: Any) -> None:
            seen["init"] = (size, kwargs)

        def transcribe(self, audio: io.BytesIO, **kwargs: Any) -> tuple[list[Any], None]:
            seen["kwargs"] = kwargs
            seg = types.SimpleNamespace
            return [seg(text=" Brief "), seg(text="me. ")], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    stt = WhisperSTT(SttConfig(model_size="tiny", language="en"))
    assert stt.transcribe(b"") == ""
    assert stt.transcribe(b"audio") == "Brief me."
    assert seen["init"] == ("tiny", {"device": "cpu", "compute_type": "int8"})
    assert seen["kwargs"] == {"language": "en", "vad_filter": True, "beam_size": 1}


def test_whisper_warm_loads_only_a_downloaded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    class Model:
        def __init__(self, size: str, **kwargs: Any) -> None:
            seen.append(kwargs)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    stt = WhisperSTT(SttConfig())
    stt.warm().join(5)
    assert seen == [{"device": "cpu", "compute_type": "int8", "local_files_only": True}]
    stt.transcribe(b"")  # loaded once, reused

    class NotDownloaded:
        def __init__(self, size: str, **kwargs: Any) -> None:
            raise OSError("not in the local cache")

    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=NotDownloaded)
    )
    missing = WhisperSTT(SttConfig())
    missing.warm().join(5)  # logged, not raised
    assert missing._model is None


def test_missing_engines_raise_voice_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(VoiceUnavailableError, match="voice"):
        WhisperSTT(SttConfig()).transcribe(b"x")
    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace())
    with pytest.raises(VoiceUnavailableError, match="not found"):
        PiperTTS(TtsConfig(), tmp_path).synthesize("hi")


def test_piper_tts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "en_US-lessac-medium.onnx").write_bytes(b"model")

    class Voice:
        @classmethod
        def load(cls, path: str) -> Voice:
            assert path.endswith("en_US-lessac-medium.onnx")
            return cls()

        def synthesize_wav(self, text: str, w: wave.Wave_write) -> None:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00\x00" * 10)

    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=Voice))
    tts = build_tts(TtsConfig(), tmp_path)
    assert isinstance(tts, PiperTTS)
    with wave.open(io.BytesIO(tts.synthesize("Good morning"))) as w:
        assert w.getnframes() == 10
    direct = PiperTTS(TtsConfig(voice=str(tmp_path / "custom.onnx")), tmp_path)
    assert direct.model_path() == tmp_path / "custom.onnx"


def test_kokoro_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    class Pipeline:
        def __init__(self, lang_code: str) -> None:
            assert lang_code == "a"

        def __call__(self, text: str, voice: str) -> Iterator[tuple[str, str, list[float]]]:
            yield "g", "p", [0.0, 0.5]

    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=Pipeline))
    tts = build_tts(TtsConfig(engine="kokoro", voice="af_heart"), Path("."))
    assert isinstance(tts, KokoroTTS)
    with wave.open(io.BytesIO(tts.synthesize("hi"))) as w:
        assert (w.getframerate(), w.getnframes()) == (24000, 2)


def test_wav_to_ogg(tmp_path: Path) -> None:
    assert wav_to_ogg(b"x", str(tmp_path / "no-ffmpeg")) is None
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    wav = pcm16_wav(float_to_pcm16([0.0] * 16000), 16000)
    ogg = wav_to_ogg(wav)
    assert ogg is not None and ogg.startswith(b"OggS")
    assert wav_to_ogg(b"not audio") is None


def test_vapid_key_script() -> None:
    import runpy

    script = Path(__file__).resolve().parents[3] / "scripts" / "generate_vapid_keys.py"
    module = runpy.run_path(str(script))
    public, private = module["generate"]()
    assert len(public) == 87 and public.startswith("B")  # 65-byte uncompressed point
    assert len(private) == 43  # 32 bytes
