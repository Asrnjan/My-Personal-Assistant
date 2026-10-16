from __future__ import annotations

import io
import re
import sys
import types
from typing import Any

import numpy as np
import pytest

from assistant_desktop import tray
from assistant_desktop.hotkey import GlobalHotkey, HotkeyError, parse_hotkey
from assistant_desktop.tray import orb_pixels
from assistant_desktop.ui import STATE_COLORS, StatusLine, blend, gradient, supports_unicode
from assistant_desktop.wakeword import State

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")


class TtyStream(io.StringIO):
    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    def isatty(self) -> bool:
        return True


def plain(text: str) -> str:
    return ANSI.sub("", text)


# -- status line --------------------------------------------------------------


def test_live_status_line_uses_state_colours() -> None:
    stream = TtyStream()
    status = StatusLine(stream, env={"TERM": "xterm-256color"})
    assert status.live and status.color and status.unicode
    status.set_state(State.LISTENING, "hotkey")
    out = stream.getvalue()
    r, g, b = STATE_COLORS[State.LISTENING]
    assert f"\x1b[38;2;{r};{g};{b}m" in out
    assert out.startswith("\r\x1b[2K")
    text = plain(out)
    assert "LISTENING" in text and "hotkey" in text and "JARVIS" in text
    assert "\n" not in out  # redrawn in place


def test_animation_frames_change_glyphs() -> None:
    stream = TtyStream()
    status = StatusLine(stream, env={})
    status.set_state(State.LISTENING)
    first = plain(status.render())
    status.tick()
    assert plain(status.render()) != first
    status.set_state(State.THINKING)
    assert "THINKING" in plain(status.render())
    status.set_state(State.IDLE)
    before = stream.getvalue()
    status.tick()  # idle does not animate
    assert stream.getvalue() == before


def test_connection_indicator() -> None:
    stream = TtyStream()
    status = StatusLine(stream, env={})
    status.set_connection(True)
    assert "online" in plain(status.render())
    status.set_connection(True)  # unchanged: no redraw
    status.set_connection(False, "retrying")
    assert "offline" in plain(status.render())


def test_messages_on_live_terminal() -> None:
    stream = TtyStream()
    status = StatusLine(stream, env={}, name="NOVA")
    status.message("you", "what's next")
    status.message("assistant", "Board call at 4.")
    status.message("weird-role", "fallback style")
    text = plain(stream.getvalue())
    assert "YOU" in text and "what's next" in text
    assert "NOVA" in text and "Board call at 4." in text
    assert "INFO" in text
    status.close()
    assert stream.getvalue().endswith("\r\x1b[2K")


def test_plain_output_when_not_a_tty() -> None:
    stream = io.StringIO()
    status = StatusLine(stream, env={})
    assert not status.live and not status.color
    status.set_state(State.LISTENING, "hotkey")
    status.set_state(State.LISTENING, "hotkey")  # duplicate suppressed
    status.set_connection(False, "retrying in 2s")
    status.set_connection(True)
    status.tick()
    status.message("assistant", "Hello")
    status.banner("press Enter")
    status.close()
    assert stream.getvalue() == (
        "[listening] hotkey\n"
        "[backend] retrying in 2s\n"
        "[backend] online\n"
        "jarvis: Hello\n"
        "JARVIS - Chief of Staff\npress Enter\n"
    )
    assert "\x1b" not in stream.getvalue()
    assert status.render() == "[listening] hotkey (online)"


def test_no_color_env_and_dumb_term() -> None:
    assert StatusLine(TtyStream(), env={"NO_COLOR": "1"}).color is False
    assert StatusLine(TtyStream(), env={"TERM": "dumb"}).color is False
    assert StatusLine(io.StringIO(), color="always", env={}).color is True
    assert StatusLine(TtyStream(), color="never", env={}).color is False


def test_forced_colour_without_tty_prints_lines() -> None:
    stream = io.StringIO()
    status = StatusLine(stream, color="always", env={})
    status.set_state(State.SPEAKING, "replying")
    status.message("error", "boom")
    out = stream.getvalue()
    assert out.count("\n") == 2
    assert "\x1b[38;2;" in out


def test_ascii_fallback() -> None:
    stream = TtyStream(encoding="ascii")
    status = StatusLine(stream, env={})
    assert status.unicode is False
    for state in State:
        status.set_state(state)
        status.tick()
        plain(status.render()).encode("ascii")
    status.set_connection(True)
    status.message("you", "hi")
    status.banner("sub")
    stream.getvalue().encode("ascii")


def test_banner_is_gradient() -> None:
    stream = TtyStream()
    StatusLine(stream, env={}).banner("say 'hey jarvis'")
    out = stream.getvalue()
    assert "CHIEF OF STAFF" in plain(out)
    assert out.count("\x1b[38;2;") > 40


def test_colour_helpers() -> None:
    assert blend((0, 0, 0), (255, 255, 255), 0.5) == (128, 128, 128)
    assert blend((0, 0, 0), (10, 10, 10), 7) == (10, 10, 10)
    assert gradient("", (0, 0, 0), (1, 1, 1)) == ""
    assert plain(gradient("ab", (0, 0, 0), (9, 9, 9))) == "ab"

    class NoEncoding(io.StringIO):
        encoding = "no-such-codec"  # type: ignore[assignment]

    assert supports_unicode(NoEncoding()) is False


# -- tray orb -------------------------------------------------------------------


@pytest.mark.parametrize("state", list(State))
def test_orb_pixels_glow_in_state_colour(state: State) -> None:
    pixels = orb_pixels(state, size=64)
    assert pixels.shape == (64, 64, 4) and pixels.dtype == np.uint8
    assert pixels[0, 0, 3] < 10  # transparent corner
    centre = pixels[32, 32]
    assert centre[3] == 255  # opaque body
    # The body ring (between the hot spot and the edge) carries the state colour.
    sample = pixels[32, 32 + 12, :3].astype(int)
    target = np.array(STATE_COLORS[state])
    dominant = int(np.argmax(target))
    assert int(np.argmax(sample)) == dominant
    # A halo surrounds the body: partly transparent pixels outside it.
    halo = pixels[32, 32 + 19, 3]
    assert 0 < halo < 255


def test_orb_pulses_only_when_active() -> None:
    idle_a = orb_pixels(State.IDLE, 32, phase=0.0)
    idle_b = orb_pixels(State.IDLE, 32, phase=1.5)
    assert np.array_equal(idle_a, idle_b)
    listen_a = orb_pixels(State.LISTENING, 32, phase=0.0)
    listen_b = orb_pixels(State.LISTENING, 32, phase=1.5)
    assert not np.array_equal(listen_a, listen_b)
    with pytest.raises(ValueError):
        orb_pixels(State.IDLE, 4)


def test_orb_image_and_tray_available(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[Any, str]] = []

    class Image:
        @staticmethod
        def fromarray(array: Any, mode: str) -> tuple[Any, str]:
            created.append((array, mode))
            return (array, mode)

    pil = types.ModuleType("PIL")
    pil.Image = Image  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", Image)  # type: ignore[arg-type]
    image = tray.orb_image(State.SPEAKING, 16)
    assert image[1] == "RGBA" and image[0].shape == (16, 16, 4)
    monkeypatch.setitem(sys.modules, "pystray", types.ModuleType("pystray"))
    assert tray.tray_available() is True
    monkeypatch.setitem(sys.modules, "pystray", None)
    assert tray.tray_available() is False


# -- hotkey -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("ctrl+alt+space", "<ctrl>+<alt>+<space>"),
        ("Ctrl + Shift + J", "<ctrl>+<shift>+j"),
        ("cmd+option+escape", "<cmd>+<alt>+<esc>"),
        ("win+f12", "<cmd>+<f12>"),
        ("f9", "<f9>"),
    ],
)
def test_parse_hotkey(combo: str, expected: str) -> None:
    assert parse_hotkey(combo) == expected


@pytest.mark.parametrize(
    "combo", ["", "ctrl+", "hyper+a", "ctrl+ctrl+a", "ctrl+alt", "ctrl+bogus", "ctrl+\x00"]
)
def test_parse_hotkey_rejects(combo: str) -> None:
    with pytest.raises(HotkeyError):
        parse_hotkey(combo)


def test_global_hotkey_with_fake_pynput(monkeypatch: pytest.MonkeyPatch) -> None:
    listeners: list[Any] = []

    class GlobalHotKeys:
        def __init__(self, mapping: dict[str, Any]) -> None:
            self.mapping = mapping
            self.running = False
            listeners.append(self)

        def start(self) -> None:
            self.running = True

        def stop(self) -> None:
            self.running = False

    pynput = types.ModuleType("pynput")
    pynput.keyboard = types.SimpleNamespace(GlobalHotKeys=GlobalHotKeys)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pynput", pynput)
    presses: list[int] = []

    def on_press() -> None:
        presses.append(1)
        if len(presses) == 2:
            raise RuntimeError("callback bug is contained")

    hotkey = GlobalHotkey("ctrl+alt+space", on_press)
    assert hotkey.start() is True
    listener = listeners[0]
    assert listener.running
    fire = listener.mapping["<ctrl>+<alt>+<space>"]
    fire()
    fire()
    assert presses == [1, 1]
    hotkey.stop()
    hotkey.stop()
    assert not listener.running


def test_global_hotkey_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pynput", None)
    assert GlobalHotkey("ctrl+alt+space", lambda: None).start() is False
