"""Application settings.

Secrets and environment-specific values come from environment variables (or a
``.env`` file) via :class:`Settings`. Behavioural configuration that a user
edits by hand lives in ``config.yaml`` and is loaded into :class:`AppConfig`.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.example.yaml"
DEFAULT_RUBRIC_PATH = CONFIG_DIR / "rubric.example.yaml"
DEFAULT_CONTACTS_PATH = CONFIG_DIR / "contacts.example.yaml"
DEFAULT_POLICY_PATH = CONFIG_DIR / "approval_policy.example.yaml"


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


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelsConfig(_Strict):
    bulk: str = "claude-haiku-5-5"
    conversation: str = "claude-sonnet-5-5"
    bulk_effort: Effort = "low"
    conversation_effort: Effort = "medium"
    max_retries: int = Field(default=3, ge=0, le=10)
    # Re-run a refused conversation request on a fallback model (server-side, Claude API).
    refusal_fallback: bool = True


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


class IngestConfig(_Strict):
    # Meetings are pulled for today plus this many following days.
    days_ahead: int = Field(default=1, ge=0, le=14)
    # On the first email sync, look back this far.
    email_lookback_hours: int = Field(default=48, ge=1, le=24 * 30)
    include_email: bool = True


class BriefingConfig(_Strict):
    # Spoken briefs are trimmed to fit this; "tell me more" gives the rest.
    max_spoken_seconds: int = Field(default=45, ge=10, le=600)
    words_per_minute: int = Field(default=150, ge=60, le=300)

    @property
    def max_words(self) -> int:
        return self.max_spoken_seconds * self.words_per_minute // 60


class SchedulerConfig(_Strict):
    # Background sync + P1 alert check; 0 turns the periodic sync off.
    sync_interval_minutes: int = Field(default=15, ge=0, le=24 * 60)


class AppConfig(_Strict):
    timezone: str = "Asia/Kolkata"
    brief_times: BriefTimes = Field(default_factory=BriefTimes)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    wake_word: WakeWordConfig = Field(default_factory=WakeWordConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)

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
    rubric_path: Path = DEFAULT_RUBRIC_PATH
    contacts_path: Path = DEFAULT_CONTACTS_PATH
    approval_policy_path: Path = DEFAULT_POLICY_PATH
    log_level: str = "INFO"
    # Run the in-process scheduler; set false when cron or GitHub Actions drives it instead.
    scheduler_enabled: bool = True
    api_auth_token: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    fernet_key: SecretStr = SecretStr("")
    # Base URL the OAuth providers redirect back to, e.g. https://assistant.example.com
    public_base_url: str = "http://127.0.0.1:8000"
    # The CEO's own address, for notices sent by email (e.g. a channel failed).
    ceo_email: str = ""

    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    ms_client_id: str = ""
    ms_client_secret: SecretStr = SecretStr("")
    ms_tenant_id: str = "common"

    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: SecretStr = SecretStr("")
    asana_access_token: SecretStr = SecretStr("")
    asana_workspace_gid: str = ""
    clickup_api_token: SecretStr = SecretStr("")
    clickup_team_id: str = ""
    clickup_user_id: str = ""
    clickup_done_status: str = "complete"


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
