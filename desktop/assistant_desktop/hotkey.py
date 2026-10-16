"""Push-to-talk hotkey: parse ``ctrl+alt+space`` style combos and listen globally.

The global listener uses pynput (the ``voice`` extra), imported only when the
listener starts. On Linux under Wayland global hotkeys are usually blocked; the
CLI mode's Enter key is the fallback there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

_MODIFIERS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "cmd": "cmd",
    "command": "cmd",
    "super": "cmd",
    "win": "cmd",
    "meta": "cmd",
}
_NAMED_KEYS = {
    "space",
    "enter",
    "tab",
    "esc",
    "backspace",
    "delete",
    "home",
    "end",
    "page_up",
    "page_down",
    "up",
    "down",
    "left",
    "right",
    "insert",
    "pause",
    *(f"f{n}" for n in range(1, 21)),
}
_ALIASES = {"escape": "esc", "return": "enter", "pgup": "page_up", "pgdn": "page_down"}


class HotkeyError(ValueError):
    """The hotkey string cannot be parsed."""


def parse_hotkey(combo: str) -> str:
    """Translate ``ctrl+alt+space`` into pynput's ``<ctrl>+<alt>+<space>``."""
    parts = [p.strip().lower() for p in combo.split("+")]
    if not combo.strip() or any(not p for p in parts):
        raise HotkeyError(f"invalid hotkey {combo!r}")
    *mods, key = parts
    seen: list[str] = []
    for mod in mods:
        if mod not in _MODIFIERS:
            raise HotkeyError(f"unknown modifier {mod!r} in {combo!r}")
        canonical = _MODIFIERS[mod]
        if canonical in seen:
            raise HotkeyError(f"modifier {mod!r} repeated in {combo!r}")
        seen.append(canonical)
    key = _ALIASES.get(key, key)
    if key in _MODIFIERS:
        raise HotkeyError(f"hotkey {combo!r} needs a non-modifier key last")
    if key in _NAMED_KEYS:
        key_token = f"<{key}>"
    elif len(key) == 1 and key.isprintable():
        key_token = key
    else:
        raise HotkeyError(f"unknown key {key!r} in {combo!r}")
    return "+".join([*(f"<{m}>" for m in seen), key_token])


class GlobalHotkey:
    """Calls ``callback`` whenever the combo is pressed, from pynput's thread."""

    def __init__(self, combo: str, callback: Callable[[], object]) -> None:
        self.combo = combo
        self._pynput_combo = parse_hotkey(combo)
        self._callback = callback
        self._listener: Any = None

    def _fire(self) -> None:
        try:
            self._callback()
        except Exception:
            log.exception("hotkey callback failed")

    def start(self) -> bool:
        """Start listening; returns False when no global hotkey is possible here."""
        try:
            from pynput import keyboard
        except Exception as exc:  # ImportError, or no display server on Linux
            log.warning("global hotkey unavailable (%s); use the Enter key in CLI mode", exc)
            return False
        listener = keyboard.GlobalHotKeys({self._pynput_combo: self._fire})
        listener.start()
        self._listener = listener
        return True

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
