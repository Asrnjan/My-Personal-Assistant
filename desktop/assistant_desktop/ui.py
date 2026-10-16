"""Futuristic terminal status line for CLI mode.

On a colour TTY it draws a single live line: a neon orb glyph in the state's
colour, a gradient wordmark, the state and an animated waveform, redrawn in
place. Without a TTY (pipes, logs, ``NO_COLOR``) it prints plain lines such as
``[listening] hotkey`` once per change, so output stays readable and greppable.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Mapping
from typing import TextIO

from assistant_desktop.wakeword import State

RGB = tuple[int, int, int]

STATE_COLORS: dict[State, RGB] = {
    State.IDLE: (0, 229, 255),  # cyan
    State.LISTENING: (168, 85, 247),  # violet
    State.THINKING: (255, 183, 0),  # amber
    State.SPEAKING: (255, 46, 209),  # magenta
}
ERROR_COLOR: RGB = (255, 69, 96)
DIM: RGB = (110, 122, 148)
STATE_LABELS = {
    State.IDLE: "STANDING BY",
    State.LISTENING: "LISTENING",
    State.THINKING: "THINKING",
    State.SPEAKING: "SPEAKING",
}

_PULSE = ("◌", "○", "◎", "◉", "●", "◉", "◎", "○")
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_BARS = "▁▂▃▄▅▆▇█"
_ASCII_PULSE = (".", "o", "O", "@", "O", "o")
_ASCII_SPINNER = "|/-\\"
_ASCII_BARS = "_.-=#"

_ROLE_STYLE: dict[str, tuple[str, str, RGB]] = {
    "you": ("▸", "YOU", STATE_COLORS[State.IDLE]),
    "assistant": ("◂", "", STATE_COLORS[State.SPEAKING]),
    "brief": ("◆", "BRIEF", STATE_COLORS[State.THINKING]),
    "alert": ("◆", "ALERT", ERROR_COLOR),
    "notice": ("◆", "NOTICE", STATE_COLORS[State.THINKING]),
    "error": ("✖", "ERROR", ERROR_COLOR),
    "info": ("·", "INFO", DIM),
}


def fg(rgb: RGB) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
CLEAR_LINE = "\r\x1b[2K"


def blend(a: RGB, b: RGB, t: float) -> RGB:
    t = min(max(t, 0.0), 1.0)
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def gradient(text: str, start: RGB, end: RGB) -> str:
    if not text:
        return ""
    steps = max(len(text) - 1, 1)
    return "".join(fg(blend(start, end, i / steps)) + ch for i, ch in enumerate(text)) + RESET


def supports_unicode(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "◉⠋▁━".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class StatusLine:
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        color: str = "auto",
        name: str = "JARVIS",
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        env = os.environ if env is None else env
        isatty = getattr(self.stream, "isatty", None)
        self.tty = bool(isatty and isatty())
        if color == "always":
            self.color = True
        elif color == "never":
            self.color = False
        else:
            self.color = self.tty and "NO_COLOR" not in env and env.get("TERM") != "dumb"
        self.live = self.tty and self.color
        self.unicode = supports_unicode(self.stream)
        self.name = name
        self.state = State.IDLE
        self.detail = ""
        self.connected: bool | None = None
        self.frame = 0
        self._lock = threading.Lock()

    # -- rendering ---------------------------------------------------------

    def _orb(self, state: State) -> str:
        if not self.unicode:
            pulse = _ASCII_PULSE
            return pulse[self.frame % len(pulse)] if state is State.LISTENING else "@"
        if state is State.LISTENING:
            return _PULSE[self.frame % len(_PULSE)]
        return "◉" if state is not State.THINKING else "◎"

    def _activity(self, state: State) -> str:
        bars = _BARS if self.unicode else _ASCII_BARS
        spinner = _SPINNER if self.unicode else _ASCII_SPINNER
        if state is State.THINKING:
            return spinner[self.frame % len(spinner)]
        if state in (State.LISTENING, State.SPEAKING):
            return "".join(bars[(self.frame + i * 3) % len(bars)] for i in range(6))
        return ""

    def render(self) -> str:
        state, detail = self.state, self.detail
        label = STATE_LABELS[state]
        if not self.color:
            link = ""
            if self.connected is not None:
                link = " (online)" if self.connected else " (offline)"
            return f"[{state.value}] {detail}".rstrip() + link
        color = STATE_COLORS[state]
        sep = "━━" if self.unicode else "--"
        parts = [
            "  ",
            BOLD + fg(color) + self._orb(state) + RESET,
            "  ",
            BOLD + gradient(self.name, STATE_COLORS[State.IDLE], STATE_COLORS[State.SPEAKING]),
            " " + fg(DIM) + sep + RESET + " ",
            BOLD + fg(color) + label + RESET,
        ]
        activity = self._activity(state)
        if activity:
            parts.append("  " + fg(color) + activity + RESET)
        if detail:
            parts.append("  " + fg(DIM) + detail + RESET)
        if self.connected is not None:
            dot = ("◆" if self.connected else "◇") if self.unicode else "*"
            link_color = STATE_COLORS[State.IDLE] if self.connected else ERROR_COLOR
            text = "online" if self.connected else "offline"
            parts.append("   " + fg(link_color) + dot + " " + text + RESET)
        return "".join(parts)

    def _draw(self) -> None:
        if self.live:
            self.stream.write(CLEAR_LINE + self.render())
        else:
            self.stream.write(self.render() + "\n")
        self.stream.flush()

    # -- updates (any thread) ----------------------------------------------

    def set_state(self, state: State, detail: str = "") -> None:
        with self._lock:
            if state is self.state and detail == self.detail and not self.live:
                return
            self.state, self.detail = state, detail
            self._draw()

    def set_connection(self, connected: bool, detail: str = "") -> None:
        with self._lock:
            if connected is self.connected:
                return
            self.connected = connected
            if self.live:
                self._draw()
            else:
                self.stream.write(f"[backend] {'online' if connected else detail or 'offline'}\n")
                self.stream.flush()

    def tick(self) -> None:
        """Advance the animation; only redraws on a live terminal."""
        with self._lock:
            self.frame += 1
            if self.live and self.state is not State.IDLE:
                self._draw()

    def message(self, role: str, text: str) -> None:
        glyph, label, rgb = _ROLE_STYLE.get(role, _ROLE_STYLE["info"])
        label = label or self.name
        with self._lock:
            if self.color:
                glyph = glyph if self.unicode else ">"
                line = f"  {fg(rgb)}{BOLD}{glyph} {label:<7}{RESET} {text}"
                prefix = CLEAR_LINE if self.live else ""
                self.stream.write(prefix + line + "\n")
                if self.live:
                    self.stream.write(self.render())
            else:
                self.stream.write(f"{label.lower()}: {text}\n")
            self.stream.flush()

    def banner(self, subtitle: str) -> None:
        with self._lock:
            if self.color:
                rule = ("━" if self.unicode else "-") * 46
                dot = "·" if self.unicode else "-"
                self.stream.write(
                    "\n  "
                    + gradient(rule, STATE_COLORS[State.IDLE], STATE_COLORS[State.SPEAKING])
                    + "\n  "
                    + BOLD
                    + gradient(
                        f"{self.name}  {dot}  CHIEF OF STAFF",
                        STATE_COLORS[State.IDLE],
                        (255, 255, 255),
                    )
                    + "\n  "
                    + fg(DIM)
                    + subtitle
                    + RESET
                    + "\n  "
                    + gradient(rule, STATE_COLORS[State.SPEAKING], STATE_COLORS[State.IDLE])
                    + "\n\n"
                )
            else:
                self.stream.write(f"{self.name} - Chief of Staff\n{subtitle}\n")
            self.stream.flush()

    def close(self) -> None:
        with self._lock:
            if self.live:
                self.stream.write(CLEAR_LINE)
                self.stream.flush()
