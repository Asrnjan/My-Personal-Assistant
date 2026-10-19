from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from assistant_desktop.config import load_config

DESKTOP = Path(__file__).resolve().parent.parent


def load_build_script() -> Any:
    spec = importlib.util.spec_from_file_location("build_script", DESKTOP / "scripts" / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_config_is_valid(clean_env: dict[str, str]) -> None:
    config = load_config(DESKTOP / "desktop.example.toml", env=clean_env)
    assert config.wake.phrase == "atlas"
    assert config.tts.effective_voice == "en_US-lessac-medium"


def test_build_args() -> None:
    build = load_build_script()
    args = build.pyinstaller_args(onefile=True, platform="darwin", name="jarvis")
    assert args[0].endswith("__main__.py")
    assert "--onefile" in args and "--windowed" in args
    assert args[args.index("--name") + 1] == "jarvis"
    linux = build.pyinstaller_args(onefile=False, platform="linux", name="x")
    assert "--onedir" in linux and "--windowed" not in linux


def test_build_main(monkeypatch: pytest.MonkeyPatch) -> None:
    build = load_build_script()
    monkeypatch.setitem(sys.modules, "PyInstaller", None)
    assert build.main([]) == 1
    runs: list[list[str]] = []
    package = types.ModuleType("PyInstaller")
    entry = types.ModuleType("PyInstaller.__main__")
    entry.run = runs.append  # type: ignore[attr-defined]
    package.__main__ = entry  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PyInstaller", package)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", entry)
    assert build.main(["--onefile"]) == 0
    assert "--onefile" in runs[0]


def test_module_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.setattr(sys, "argv", ["assistant-desktop", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("assistant_desktop", run_name="__main__")
    assert exc.value.code == 0
