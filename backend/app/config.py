"""Application settings.

Secrets and environment-specific values come from environment variables (or a
``.env`` file) via :class:`Settings`. Behavioural configuration that a user
edits by hand lives in ``config.yaml`` and is loaded into :class:`AppConfig`.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.example.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BriefTimes(_Strict):
    morning: time = time(8, 0)
    evening: time = time(18, 30)


class AdaptersConfig(_Strict):
    calendar: list[str] = Field(default_factory=lambda: ["google"])
    email: list[str] = Field(default_factory=lambda: ["google"])
    tasks: list[str] = Field(default_factory=lambda: ["jira"])
    notify: list[str] = Field(default_factory=lambda: ["email", "telegram"])

    @field_validator("calendar", "email")
    @classmethod
    def _check_mail_calendar(cls, value: list[str]) -> list[str]:
        return _check_known(value, {"google", "microsoft"})

    @field_validator("tasks")
    @classmethod
    def _check_tasks(cls, value: list[str]) -> list[str]:
        return _check_known(value, {"jira", "asana", "clickup"})

    @field_validator("notify")
    @classmethod
    def _check_notify(cls, value: list[str]) -> list[str]:
        return _check_known(value, {"email", "telegram", "slack", "teams"})


def _check_known(value: list[str], allowed: set[str]) -> list[str]:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown adapter(s) {unknown}; allowed: {sorted(allowed)}")
    return value


class ModelsConfig(_Strict):
    bulk: str = "claude-haiku-5-5"
    conversation: str = "claude-sonnet-5-5"


class BudgetConfig(_Strict):
    monthly_usd: float = Field(default=20.0, ge=0)


class WakeWordConfig(_Strict):
    phrase: str = "hey jarvis"
    push_to_talk_hotkey: str = "ctrl+alt+space"


class SttConfig(_Strict):
    engine: str = "faster-whisper"
    model_size: str = "small"
    compute_type: str = "int8"
    language: str | None = None


class TtsConfig(_Strict):
    engine: str = "piper"
    voice: str = "en_US-lessac-medium"

    @field_validator("engine")
    @classmethod
    def _check_engine(cls, value: str) -> str:
        if value not in {"piper", "kokoro"}:
            raise ValueError("tts.engine must be 'piper' or 'kokoro'")
        return value


class AppConfig(_Strict):
    timezone: str = "Asia/Kolkata"
    brief_times: BriefTimes = Field(default_factory=BriefTimes)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    wake_word: WakeWordConfig = Field(default_factory=WakeWordConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Settings(BaseSettings):
    """Environment-provided settings. Secrets are wrapped in ``SecretStr``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./assistant.db"
    config_path: Path = DEFAULT_CONFIG_PATH
    log_level: str = "INFO"
    api_auth_token: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    fernet_key: SecretStr = SecretStr("")


def load_app_config(path: Path) -> AppConfig:
    """Load and validate ``config.yaml``. A missing or empty file yields defaults."""
    if not path.exists():
        return AppConfig()
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return AppConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return AppConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
