from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tone_wav() -> bytes:
    return (FIXTURES / "tone_440hz_16k.wav").read_bytes()


@pytest.fixture
def silence_wav() -> bytes:
    return (FIXTURES / "silence_16k.wav").read_bytes()


@pytest.fixture
def stereo_wav() -> bytes:
    return (FIXTURES / "tone_stereo_22k.wav").read_bytes()


@pytest.fixture
def clean_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose config dir is an empty temp dir."""
    return {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
