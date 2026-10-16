"""Regenerate the short WAV fixtures: ``python tests/fixtures/make_fixtures.py``.

Uses only the standard library so the fixtures do not depend on the code under test.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

HERE = Path(__file__).parent


def write(name: str, rate: int, channels: int, samples: list[float]) -> None:
    with wave.open(str(HERE / name), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = b"".join(struct.pack("<h", round(max(-1.0, min(1.0, s)) * 32767)) for s in samples)
        wav.writeframes(frames)


def sine(freq: float, seconds: float, rate: int, amp: float) -> list[float]:
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(round(seconds * rate))]


def main() -> None:
    write("tone_440hz_16k.wav", 16_000, 1, sine(440.0, 0.5, 16_000, 0.5))
    write("silence_16k.wav", 16_000, 1, [0.0] * 8_000)
    left = sine(440.0, 0.5, 22_050, 0.4)
    stereo = [v for s in left for v in (s, s)]
    write("tone_stereo_22k.wav", 22_050, 2, stereo)


if __name__ == "__main__":
    main()
