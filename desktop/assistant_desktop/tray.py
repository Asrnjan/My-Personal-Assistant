"""System tray icon: a glowing neon orb whose colour and pulse follow the state.

The orb is drawn with numpy (:func:`orb_pixels`) and only converted to a Pillow
image for pystray, so the drawing is testable without the ``tray`` extra.
Idle is cyan, listening a pulsing violet, thinking amber, speaking magenta.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
from numpy.typing import NDArray

from assistant_desktop.ui import STATE_COLORS, STATE_LABELS
from assistant_desktop.wakeword import State

log = logging.getLogger(__name__)

PULSING = frozenset({State.LISTENING, State.SPEAKING, State.THINKING})


def orb_pixels(state: State, size: int = 64, phase: float = 0.0) -> NDArray[np.uint8]:
    """RGBA pixels of the orb: a white-hot core, a coloured body, a soft halo and a thin ring."""
    if size < 8:
        raise ValueError("icon size must be at least 8 pixels")
    rgb = np.array(STATE_COLORS[state], dtype=np.float64) / 255.0
    pulse = 0.5 + 0.5 * math.sin(phase) if state in PULSING else 0.6
    coords = (np.arange(size, dtype=np.float64) + 0.5) / size * 2.0 - 1.0
    x, y = np.meshgrid(coords, coords)
    r = np.sqrt(x * x + y * y)

    body_radius = 0.50 + 0.06 * pulse
    edge = 2.0 / size
    body = np.clip((body_radius - r) / edge + 0.5, 0.0, 1.0)
    outside = np.maximum(r - body_radius, 0.0)
    halo = np.exp(-((outside / (0.16 + 0.08 * pulse)) ** 2)) * (0.55 + 0.35 * pulse)
    ring_radius = 0.86
    ring = np.exp(-(((r - ring_radius) / 0.035) ** 2)) * (0.35 + 0.4 * pulse)
    # Neon rim at the body's edge, and a hot spot from the upper left for depth.
    rim = np.exp(-(((r - body_radius) / 0.06) ** 2)) * body
    hot = np.exp(-(((x + 0.18) ** 2 + (y + 0.2) ** 2) / 0.06)) * body

    alpha = np.clip(np.maximum(np.maximum(body, halo), ring), 0.0, 1.0)
    depth = 0.85 + 0.15 * (1.0 - np.clip(r / body_radius, 0.0, 1.0))
    color = rgb[None, None, :] * np.where(body > 0, depth, 1.0)[..., None]
    color = color + (rim * 0.35)[..., None] * (1.0 - color)
    color = color + (hot * 0.85)[..., None] * (1.0 - color)
    pixels = np.dstack([np.clip(color, 0.0, 1.0), alpha])
    out: NDArray[np.uint8] = np.round(pixels * 255.0).astype(np.uint8)
    return out


def orb_image(state: State, size: int = 64, phase: float = 0.0) -> Any:
    """The orb as a Pillow image (needs the ``tray`` extra)."""
    from PIL import Image

    return Image.fromarray(orb_pixels(state, size, phase), mode="RGBA")


def tray_available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:  # ImportError, or no usable backend on this platform
        return False
    return True


class TrayApp:  # pragma: no cover - thin pystray glue, needs a desktop session
    """pystray icon with Talk / Chime / Quit; :meth:`run` blocks the main thread."""

    def __init__(
        self,
        *,
        name: str,
        on_talk: Callable[[], object],
        on_quit: Callable[[], None],
        chime: Callable[[], bool],
        toggle_chime: Callable[[], None],
    ) -> None:
        import pystray

        self._pystray = pystray
        self._state = State.IDLE
        self._detail = ""
        self._phase = 0.0
        self._stop = threading.Event()
        self._on_quit = on_quit
        self._name = name
        menu = pystray.Menu(
            pystray.MenuItem("Talk now", lambda: on_talk(), default=True),
            pystray.MenuItem(
                "Chime before briefs", lambda: toggle_chime(), checked=lambda _item: chime()
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon("ceo-assistant", orb_image(State.IDLE), self._title(), menu=menu)

    def _title(self) -> str:
        detail = f" · {self._detail}" if self._detail else ""
        return f"{self._name} · {STATE_LABELS[self._state].title()}{detail}"

    def set_state(self, state: State, detail: str = "") -> None:
        self._state, self._detail = state, detail
        self.icon.icon = orb_image(state, phase=self._phase)
        self.icon.title = self._title()

    def notify(self, title: str, text: str) -> None:
        try:
            self.icon.notify(text[:250], title)
        except Exception as exc:
            log.debug("tray notification failed: %s", exc)

    def _animate(self) -> None:
        while not self._stop.wait(0.12):
            if self._state in PULSING:
                self._phase += 0.6
                self.icon.icon = orb_image(self._state, phase=self._phase)

    def _quit(self) -> None:
        self._stop.set()
        self._on_quit()
        self.icon.stop()

    def run(self, setup: Callable[[], None]) -> None:
        def _setup(icon: Any) -> None:
            icon.visible = True
            threading.Thread(target=self._animate, name="tray-orb", daemon=True).start()
            setup()

        self.icon.run(setup=_setup)
