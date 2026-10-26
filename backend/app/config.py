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
DEFAULT_PWA_DIR = REPO_ROOT / "mobile-pwa"


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
    # Spoken back-and-forth: the fastest model, so a reply starts in about a second.
    conversation: str = "claude-haiku-5-5"
    # Work that needs more judgment: decisions, drafting, meeting notes, delegation briefs
    # and second opinions on priorities.
    reasoning: str = "claude-sonnet-5-5"
    bulk_effort: Effort = "low"
    # "low" keeps spoken replies quick; raise it for more careful (slower) answers.
    conversation_effort: Effort = "low"
    reasoning_effort: Effort = "low"
    max_retries: int = Field(default=3, ge=0, le=10)
    # Re-run a refused conversation request on a fallback model (server-side, Claude API).
    refusal_fallback: bool = True
    # Quick spoken replies: no extended thinking in conversation where the model allows it
    # (thinking off on Claude Haiku 5.5, "between_tools" on Claude Sonnet 5.5).
    fast_replies: bool = True
    # Let the conversation search the web for news, facts, companies and people.
    web_search: bool = True
    web_searches_per_turn: int = Field(default=3, ge=1, le=10)


class BudgetConfig(_Strict):
    monthly_usd: float = Field(default=20.0, ge=0)


class WakeWordConfig(_Strict):
    phrase: str = "atlas"
    push_to_talk_hotkey: str = "ctrl+alt+space"


class SttConfig(_Strict):
    engine: str = "faster-whisper"
    # English-only "base" transcribes a short request in about a second on a laptop CPU;
    # "small" or "small.en" are more accurate but several times slower.
    model_size: str = "base.en"
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


class WorkdayConfig(_Strict):
    """The CEO's working hours, for spotting a day with no time to think."""

    start: time = time(9, 0)
    end: time = time(18, 0)
    # A day without a free block this long gets a "no focus time" warning.
    focus_minutes: int = Field(default=90, ge=15, le=480)
    # Meeting prep is sent this many minutes before a meeting; 0 turns it off.
    prep_minutes_before: int = Field(default=10, ge=0, le=120)
    # The weekly team pulse and activity report.
    weekly_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "fri"
    weekly_time: time = time(17, 0)


class SchedulerConfig(_Strict):
    # Background sync + P1 alert check; 0 turns the periodic sync off.
    sync_interval_minutes: int = Field(default=15, ge=0, le=24 * 60)


class AppConfig(_Strict):
    # What the CEO calls the assistant; the apps listen for it and it answers to it.
    assistant_name: str = Field(default="Atlas", min_length=1, max_length=30)
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
    workday: WorkdayConfig = Field(default_factory=WorkdayConfig)

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

    # The repository-root .env, then one in the working directory (later wins).
    model_config = SettingsConfigDict(env_file=(REPO_ROOT / ".env", ".env"), extra="ignore")

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

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_ceo_chat_id: str = ""
    # Long-poll Telegram for button presses and the CEO's messages (needs the bot token).
    telegram_polling: bool = True
    slack_bot_token: SecretStr = SecretStr("")
    slack_ceo_user_id: str = ""
    # Incoming-webhook URL of a Teams channel the CEO reads.
    teams_webhook_url: SecretStr = SecretStr("")

    vapid_public_key: str = ""
    vapid_private_key: SecretStr = SecretStr("")
    vapid_subject: str = "mailto:assistant@example.com"

    # Folder holding Piper ``.onnx`` voices (docs/SETUP.md, "Voices").
    piper_voices_dir: Path = Path("voices")
    # Where presentations and other files the assistant makes are kept.
    files_dir: Path = Path("files")
    # Requests a minute per client (bearer token, else address); 0 turns limiting off.
    rate_limit_per_minute: int = 120
    # Tighter limit on POST /auth/token per address, against guessing the shared secret.
    auth_rate_limit_per_minute: int = 10
    # Read the client address from CF-Connecting-IP / X-Forwarded-For (behind a tunnel).
    trust_proxy_headers: bool = False
    # Comma-separated origins allowed to call the API from a browser, e.g. a PWA
    # hosted elsewhere. Empty: same origin only.
    cors_origins: str = ""

    # The installable web app, served at ``/``; empty disables it.
    pwa_dir: Path | None = DEFAULT_PWA_DIR

    @field_validator(
        "config_path",
        "rubric_path",
        "contacts_path",
        "approval_policy_path",
        "piper_voices_dir",
        "files_dir",
        "pwa_dir",
        mode="before",
    )
    @classmethod
    def _from_repo_root(cls, value: str | Path | None) -> Path | None:
        """Relative paths are relative to the repository root, wherever the app starts.
        An empty value (only meaningful for ``PWA_DIR``) turns the feature off."""
        if value is None or str(value).strip() == "":
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    def secret_values(self) -> list[str]:
        """Every configured secret, for log redaction."""
        return [
            v.get_secret_value()
            for v in (
                self.api_auth_token,
                self.anthropic_api_key,
                self.fernet_key,
                self.google_client_secret,
                self.ms_client_secret,
                self.jira_api_token,
                self.asana_access_token,
                self.clickup_api_token,
                self.telegram_bot_token,
                self.slack_bot_token,
                self.teams_webhook_url,
                self.vapid_private_key,
            )
            if v.get_secret_value()
        ]


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
