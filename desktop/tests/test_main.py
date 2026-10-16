from __future__ import annotations

import io
import json
import signal
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from assistant_desktop import main as main_module
from assistant_desktop.auth import AuthError, BackendError, TokenProvider
from assistant_desktop.client import BackendClient, PushEvent
from assistant_desktop.config import DesktopConfig, load_config
from assistant_desktop.main import (
    DesktopApp,
    build_speaker,
    build_voice_input,
    default_mode,
    friendly_error,
    main,
    run_cli,
)
from assistant_desktop.stt import SttUnavailableError
from assistant_desktop.tts import TextSpeaker, VoiceSpeaker
from assistant_desktop.ui import StatusLine
from assistant_desktop.wakeword import State

BASE = "http://backend.test"


def backend(*, healthy: bool = True, converse: dict[str, Any] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/token":
            return httpx.Response(200, json={"access_token": "tok"})
        if path == "/health":
            if not healthy:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"status": "ok", "version": "0.1.0"})
        if path == "/auth/whoami":
            return httpx.Response(200, json={"client_name": "desktop"})
        if path == "/converse":
            if not healthy:
                raise httpx.ConnectError("refused", request=request)
            text = json.loads(request.content)["text"]
            body = converse or {"conversation_id": "c1", "reply": f"Echo: {text}"}
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def refuse(_url: str) -> Any:
    raise ConnectionRefusedError("no websocket in tests")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "CEO_DESKTOP_BACKEND_URL": BASE,
        "CEO_DESKTOP_SECRET": "shared-secret",
    }


@pytest.fixture(autouse=True)
def keep_sigterm() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


class FakeSpeaker:
    def __init__(self, fail: bool = False) -> None:
        self.spoken: list[str] = []
        self.fail = fail

    def speak(self, text: str, *, chime: bool = False) -> bool:
        if self.fail:
            raise RuntimeError("no audio")
        self.spoken.append(text)
        return True

    def stop(self) -> None:
        return None


# -- --check / --once -----------------------------------------------------------


def test_check_ok(env: dict[str, str]) -> None:
    out = io.StringIO()
    code = main(["--check"], env=env, http_transport=backend(), stdout=out)
    text = out.getvalue()
    assert code == 0
    assert "shared-secret" not in text
    assert "[ok  ] health" in text
    assert "[ok  ] auth" in text
    assert "Optional components" in text
    assert "pip extra [voice]" in text or "installed" in text


def test_check_backend_down(env: dict[str, str]) -> None:
    out = io.StringIO()
    assert main(["--check"], env=env, http_transport=backend(healthy=False), stdout=out) == 1
    assert "[FAIL] health" in out.getvalue()


def test_check_without_secret(env: dict[str, str]) -> None:
    env = {k: v for k, v in env.items() if k != "CEO_DESKTOP_SECRET"}
    out = io.StringIO()
    assert main(["--check"], env=env, http_transport=backend(), stdout=out) == 1
    assert "[FAIL] auth" in out.getvalue()


def test_config_error_exit_code(env: dict[str, str]) -> None:
    out = io.StringIO()
    assert main(["--check"], env={**env, "CEO_DESKTOP_STT_COMPUTE_TYPE": "x"}, stdout=out) == 2
    assert "configuration error" in out.getvalue()


def test_once_prints_reply(env: dict[str, str]) -> None:
    out = io.StringIO()
    code = main(["--once", "what's next"], env=env, http_transport=backend(), stdout=out)
    assert code == 0
    assert out.getvalue() == "jarvis: Echo: what's next\n"
    # The token was exchanged once and cached for the next run.
    cache = Path(env["XDG_CONFIG_HOME"]) / "ceo-assistant" / "desktop-token.json"
    assert json.loads(cache.read_text())["access_token"] == "tok"


def test_once_needs_confirmation_and_speaks(env: dict[str, str]) -> None:
    out = io.StringIO()
    speaker = FakeSpeaker()
    code = main(
        ["--once", "decline the 4 pm", "--speak"],
        env=env,
        http_transport=backend(converse={"reply": "Decline it?", "needs_confirmation": True}),
        stdout=out,
        speaker_factory=lambda _c: speaker,
    )
    assert code == 0
    assert "Awaiting your confirmation" in out.getvalue()
    assert speaker.spoken == ["Decline it?"]


def test_once_speak_failure_is_reported(env: dict[str, str]) -> None:
    out = io.StringIO()
    code = main(
        ["--once", "hi", "--speak"],
        env=env,
        http_transport=backend(),
        stdout=out,
        speaker_factory=lambda _c: FakeSpeaker(fail=True),
    )
    assert code == 0
    assert "could not speak" in out.getvalue()


def test_once_backend_down(env: dict[str, str]) -> None:
    out = io.StringIO()
    assert main(["--once", "hi"], env=env, http_transport=backend(healthy=False), stdout=out) == 1
    assert out.getvalue().startswith("error: cannot reach")


# -- CLI mode -------------------------------------------------------------------------


def wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_cli_text_session(env: dict[str, str]) -> None:
    out = io.StringIO()

    def lines() -> Iterator[str]:
        yield "/help\n"
        yield "\n"  # push-to-talk without a microphone
        yield "brief me\n"
        wait_for(lambda: "Echo: brief me" in out.getvalue())
        yield "/quit\n"

    code = main(
        ["--cli", "--text-only"],
        env=env,
        http_transport=backend(),
        connector=refuse,
        stdin=lines(),
        stdout=out,
        speaker_factory=lambda _c: TextSpeaker(),
    )
    text = out.getvalue()
    assert code == 0
    assert "type a request" in text
    assert "info: Enter = push-to-talk" in text
    assert "Voice input is not available" in text
    assert "you: brief me" in text
    assert "jarvis: Echo: brief me" in text
    assert "[thinking] typed" in text
    assert "[idle] done" in text
    assert signal.getsignal(signal.SIGTERM) is main_module._terminate


def make_app(
    env: dict[str, str], speaker: Any = None
) -> tuple[DesktopApp, StatusLine, io.StringIO]:
    config = load_config(env=env)
    http = httpx.Client(transport=backend())
    client = BackendClient(BASE, TokenProvider(http, BASE, secret="s"), http, connector=refuse)
    out = io.StringIO()
    status = StatusLine(out, env={})
    app = DesktopApp(
        config,
        client,
        speaker=speaker or TextSpeaker(),
        on_state=status.set_state,
        on_message=status.message,
        on_connection=status.set_connection,
    )
    return app, status, out


def test_cli_busy_and_eof(env: dict[str, str]) -> None:
    app, status, out = make_app(env)
    release = threading.Event()

    def slow_ask(text: str) -> str:
        release.wait(2)
        return "slow reply"

    app.ask = slow_ask  # type: ignore[method-assign]
    app.handler._ask = slow_ask

    def lines() -> Iterator[str]:
        yield "first\n"
        yield "second\n"  # rejected: still thinking
        release.set()
        wait_for(lambda: "slow reply" in out.getvalue())
        threading.Timer(0.05, app.request_stop).start()

    assert run_cli(app, status, lines()) == 0
    text = out.getvalue()
    assert "Still working on the last request" in text
    assert "Input closed; still listening" in text


def test_cli_keyboard_interrupt(env: dict[str, str]) -> None:
    app, status, out = make_app(env)

    def lines() -> Iterator[str]:
        raise KeyboardInterrupt
        yield ""  # pragma: no cover - makes this a generator

    assert run_cli(app, status, lines()) == 0
    assert "Shutting down." in out.getvalue()


def test_cli_live_terminal_ticker(env: dict[str, str]) -> None:
    app, _, _ = make_app(env)

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = Tty()
    status = StatusLine(stream, env={})

    def lines() -> Iterator[str]:
        time.sleep(0.3)  # let the ticker run a couple of frames
        yield "/q\n"

    assert run_cli(app, status, lines()) == 0
    assert status.frame >= 1


def test_app_announces_push_events(env: dict[str, str]) -> None:
    speaker = FakeSpeaker()
    app, _, out = make_app(env, speaker)
    app.on_event(PushEvent("brief", "Good morning.", {"text": "Good morning."}))
    while app.handler.process_next():
        continue
    assert speaker.spoken == ["Good morning."]
    assert "brief: Good morning." in out.getvalue()
    assert app.handler.state is State.IDLE


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        ("morning", "brief"),
        ("evening", "brief"),
        ("brief", "brief"),
        ("alert", "alert"),
        ("notice", "notice"),
        ("other", "notice"),
    ],
)
def test_event_role(kind: str, role: str) -> None:
    assert main_module.event_role(kind) == role


def test_app_ask_defaults_empty_reply(env: dict[str, str]) -> None:
    app, _, _ = make_app(env)
    app.client.converse = lambda _t: types.SimpleNamespace(text="")  # type: ignore[method-assign]
    assert app.ask("x") == "Done."


# -- wiring helpers ----------------------------------------------------------------------


def test_friendly_error() -> None:
    assert "authorised" in friendly_error(AuthError("x"))
    assert "backend" in friendly_error(BackendError("x"))
    assert "microphone" in friendly_error(main_module.AudioUnavailableError("x"))
    assert friendly_error(SttUnavailableError("install voice")) == "install voice"
    assert friendly_error(ValueError("bad")) == "Something went wrong: bad"
    assert friendly_error(ValueError()) == "Something went wrong."


def fake_sounddevice(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    monkeypatch.setitem(
        sys.modules, "sounddevice", types.ModuleType("sounddevice") if available else None
    )


def test_build_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    config = DesktopConfig()
    monkeypatch.setattr(main_module, "module_available", lambda _name: False)
    assert isinstance(build_speaker(config), TextSpeaker)
    monkeypatch.setattr(main_module, "module_available", lambda _name: True)
    fake_sounddevice(monkeypatch, available=False)
    assert isinstance(build_speaker(config), TextSpeaker)
    fake_sounddevice(monkeypatch, available=True)
    assert isinstance(build_speaker(config), VoiceSpeaker)


def test_build_voice_input(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    config = load_config(env={**env, "CEO_DESKTOP_STT_LANGUAGE": "auto"})
    monkeypatch.setattr(main_module, "module_available", lambda _name: False)
    assert build_voice_input(config) is None
    monkeypatch.setattr(main_module, "module_available", lambda _name: True)
    fake_sounddevice(monkeypatch, available=False)
    assert build_voice_input(config) is None
    fake_sounddevice(monkeypatch, available=True)
    voice = build_voice_input(config)
    assert voice is not None
    assert voice.detector is not None and voice.detector.model_name == "hey_jarvis"
    assert voice.transcriber.model_size == "small"
    assert voice.transcriber.language is None
    endpointer = voice.recorder._factory()
    assert endpointer.silence_seconds == config.listen.silence_seconds
    assert endpointer.noise_floor_dbfs == voice.noise.value
    monkeypatch.setattr(main_module, "module_available", lambda name: name != "openwakeword")
    voice = build_voice_input(config)
    assert voice is not None and voice.detector is None


def test_module_available() -> None:
    assert main_module.module_available("json") is True
    assert main_module.module_available("definitely_not_a_module_xyz") is False
    assert main_module.module_available(".relative") is False


def test_default_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from assistant_desktop import tray

    assert default_mode("linux") == "cli"
    monkeypatch.setattr(tray, "tray_available", lambda: True)
    assert default_mode("darwin") == "tray"
    assert default_mode("win32") == "tray"
    monkeypatch.setattr(tray, "tray_available", lambda: False)
    assert default_mode("darwin") == "cli"


def test_terminate_raises_keyboard_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        main_module._terminate(15, None)


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "assistant-desktop 0.1.0" in capsys.readouterr().out
