from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_CONFIG_PATH, AppConfig, Settings, get_settings, load_app_config


def test_example_config_loads() -> None:
    cfg = load_app_config(DEFAULT_CONFIG_PATH)
    assert cfg.timezone == "Asia/Kolkata"
    assert cfg.brief_times.morning == time(8, 0)
    assert cfg.brief_times.evening == time(18, 30)
    assert cfg.models.bulk == "claude-haiku-5-5"
    assert cfg.models.conversation == "claude-sonnet-5-5"
    assert cfg.tz.key == "Asia/Kolkata"


def test_missing_and_empty_files_give_defaults(tmp_path: Path) -> None:
    assert load_app_config(tmp_path / "nope.yaml") == AppConfig()
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert load_app_config(empty) == AppConfig()


def test_partial_override(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("timezone: Europe/London\nbudget:\n  monthly_usd: 5\n")
    cfg = load_app_config(path)
    assert cfg.timezone == "Europe/London"
    assert cfg.budget.monthly_usd == 5
    assert cfg.adapters.tasks == ["jira"]


@pytest.mark.parametrize(
    "body",
    [
        "timezone: Mars/Olympus\n",
        "adapters:\n  tasks: [trello]\n",
        "adapters:\n  calendar: [yahoo]\n",
        "adapters:\n  notify: [sms]\n",
        "tts:\n  engine: elevenlabs\n",
        "budget:\n  monthly_usd: -1\n",
        "unknown_key: 1\n",
    ],
)
def test_invalid_config_rejected(tmp_path: Path, body: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(body)
    with pytest.raises(ValidationError):
        load_app_config(path)


def test_non_mapping_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="mapping"):
        load_app_config(path)


def test_settings_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "abc")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    settings = Settings()
    assert settings.api_auth_token.get_secret_value() == "abc"
    assert settings.database_url == "sqlite:///x.db"
    assert "abc" not in repr(settings)


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()


def test_relative_paths_resolve_from_the_repo_root() -> None:
    from app.config import REPO_ROOT

    s = Settings(config_path="config/config.yaml", pwa_dir="", piper_voices_dir="~/voices")
    assert s.config_path == REPO_ROOT / "config" / "config.yaml"
    assert s.pwa_dir is None
    assert s.piper_voices_dir.is_absolute() and s.piper_voices_dir.name == "voices"
