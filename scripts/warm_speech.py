"""Download the speech-to-text model once, so the first voice message from a phone is quick.

Run with the backend's Python (the Windows launcher does this after installing):

    backend/.venv/Scripts/python scripts/warm_speech.py

Uses the model named under ``stt`` in ``config/config.yaml`` (``base.en`` by default).
Prints the model name and exits 0 when it is ready; exits 1 when it can't be loaded.

    warm_speech.py --marker FILE   # skip the work when FILE already names this model,
                                   # and write the model name to FILE once it is ready
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    from pathlib import Path

    from app.config import get_settings, load_app_config
    from app.voice import VoiceUnavailableError, WhisperSTT

    stt = load_app_config(get_settings().config_path).stt
    marker = Path(argv[argv.index("--marker") + 1]) if "--marker" in argv[:-1] else None
    ready = marker.read_text(encoding="utf-8").strip() if marker and marker.exists() else ""
    if ready == stt.model_size:
        print(stt.model_size)
        return 0
    try:
        WhisperSTT(stt)._load()
    except VoiceUnavailableError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:  # network errors while downloading, a full disk, ...
        print(f"could not load the {stt.model_size} speech model: {exc}", file=sys.stderr)
        return 1
    if marker is not None:
        marker.write_text(stt.model_size, encoding="ascii")
    print(stt.model_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
