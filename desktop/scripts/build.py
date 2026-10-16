"""Build a standalone ``assistant-desktop`` executable with PyInstaller.

Run from the ``desktop/`` folder, in an environment that has the extras you
want bundled plus PyInstaller, for example::

    uv venv .build && uv pip install --python .build -e ".[voice,tray]" pyinstaller
    .build/bin/python scripts/build.py            # Windows: .build\\Scripts\\python

The result lands in ``dist/assistant-desktop/`` (``--onefile`` for one file).
Speech models are not bundled: faster-whisper, openWakeWord and Piper fetch or
read them from their usual cache folders on first run.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent.parent
ENTRY = DESKTOP / "assistant_desktop" / "__main__.py"
# Packages that load data files or plugins at runtime; collect them whole.
COLLECT = ("faster_whisper", "ctranslate2", "openwakeword", "piper", "pystray", "kokoro")


def pyinstaller_args(*, onefile: bool, platform: str, name: str) -> list[str]:
    args = [
        str(ENTRY),
        "--name",
        name,
        "--noconfirm",
        "--clean",
        "--paths",
        str(DESKTOP),
        "--distpath",
        str(DESKTOP / "dist"),
        "--workpath",
        str(DESKTOP / "build"),
        "--specpath",
        str(DESKTOP / "build"),
    ]
    args.append("--onefile" if onefile else "--onedir")
    if platform in ("win32", "darwin"):
        args.append("--windowed")  # tray app: no console window
    for package in COLLECT:
        if importlib.util.find_spec(package) is not None:
            args += ["--collect-all", package]
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--onefile", action="store_true", help="a single executable file")
    parser.add_argument("--name", default="assistant-desktop", help="executable name")
    args = parser.parse_args(argv)
    try:
        import PyInstaller.__main__
    except ImportError:
        print("PyInstaller is not installed: uv pip install pyinstaller", file=sys.stderr)
        return 1
    PyInstaller.__main__.run(
        pyinstaller_args(onefile=args.onefile, platform=sys.platform, name=args.name)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
