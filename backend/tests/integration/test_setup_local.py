from __future__ import annotations

import runpy
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def test_old_default_effort_is_moved_to_low_keeping_line_endings(tmp_path: Path) -> None:
    setup = runpy.run_path(str(SCRIPTS / "setup_local.py"))
    config = tmp_path / "config.yaml"
    config.write_bytes(
        b"models:\r\n  bulk_effort: low\r\n  conversation_effort: medium\r\n\r\nbudget: {}\r\n"
    )
    assert setup["speed_up_config"](config) is True
    assert config.read_bytes() == (
        b"models:\r\n  bulk_effort: low\r\n  conversation_effort: low\r\n\r\nbudget: {}\r\n"
    )
    assert setup["speed_up_config"](config) is False


def test_a_chosen_effort_is_left_alone(tmp_path: Path) -> None:
    setup = runpy.run_path(str(SCRIPTS / "setup_local.py"))
    config = tmp_path / "config.yaml"
    config.write_text("models:\n  conversation_effort: high\n", encoding="utf-8")
    assert setup["speed_up_config"](config) is False
    assert setup["speed_up_config"](tmp_path / "missing.yaml") is False
    assert "high" in config.read_text(encoding="utf-8")


def test_old_speech_model_default_is_moved_to_base_en(tmp_path: Path) -> None:
    setup = runpy.run_path(str(SCRIPTS / "setup_local.py"))
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n  conversation_effort: low\nstt:\n  model_size: small\n", encoding="utf-8"
    )
    assert setup["speed_up_config"](config) is True
    assert "model_size: base.en\n" in config.read_text(encoding="utf-8")
    config.write_text("stt:\n  model_size: small.en\n", encoding="utf-8")
    assert setup["speed_up_config"](config) is False
