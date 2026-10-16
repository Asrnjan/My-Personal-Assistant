"""Client configuration: a TOML file plus ``CEO_DESKTOP_*`` environment variables.

Precedence, lowest to highest: built-in defaults, the TOML file, environment
variables. The file lives at ``~/.config/ceo-assistant/desktop.toml`` (or under
``$XDG_CONFIG_HOME``); ``CEO_DESKTOP_CONFIG`` points somewhere else.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

from assistant_desktop.hotkey import HotkeyError, parse_hotkey

ENV_PREFIX = "CEO_DESKTOP_"
CONFIG_ENV = ENV_PREFIX + "CONFIG"
TOKEN_CACHE_ENV = ENV_PREFIX + "TOKEN_CACHE"
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"

TTS_ENGINES = ("piper", "kokoro")
COMPUTE_TYPES = (
    "default",
    "auto",
    "int8",
    "int8_float16",
    "int8_float32",
    "int8_bfloat16",
    "int16",
    "float16",
    "bfloat16",
    "float32",
)
COLOR_MODES = ("auto", "always", "never")
DEFAULT_VOICES = {"piper": "en_US-lessac-medium", "kokoro": "af_heart"}


class ConfigError(ValueError):
    """The configuration file or an environment variable is invalid."""


@dataclass(frozen=True)
class BackendSettings:
    url: str = DEFAULT_BACKEND_URL
    secret: str = ""
    token: str = ""
    client_name: str = "desktop"
    request_timeout_seconds: float = 30.0
    reply_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class WakeSettings:
    enabled: bool = True
    phrase: str = "hey jarvis"
    threshold: float = 0.5
    hotkey: str = "ctrl+alt+space"
    cooldown_seconds: float = 1.5


@dataclass(frozen=True)
class ListenSettings:
    silence_seconds: float = 0.8
    max_seconds: float = 15.0
    start_timeout_seconds: float = 6.0
    energy_threshold_dbfs: float = -45.0


@dataclass(frozen=True)
class SttSettings:
    model: str = "small"
    compute_type: str = "int8"
    device: str = "cpu"
    language: str = "en"
    vad_filter: bool = True
    beam_size: int = 1

    @property
    def language_code(self) -> str | None:
        """``None`` asks faster-whisper to detect the language."""
        code = self.language.strip().lower()
        return None if code in ("", "auto") else code


@dataclass(frozen=True)
class TtsSettings:
    engine: str = "piper"
    voice: str = ""
    model_dir: Path = field(
        default_factory=lambda: Path("~/.local/share/ceo-assistant/voices").expanduser()
    )
    kokoro_lang: str = "a"
    speed: float = 1.0

    @property
    def effective_voice(self) -> str:
        return self.voice or DEFAULT_VOICES[self.engine]


@dataclass(frozen=True)
class UiSettings:
    chime: bool = True
    color: str = "auto"
    name: str = "JARVIS"


@dataclass(frozen=True)
class DesktopConfig:
    backend: BackendSettings = field(default_factory=BackendSettings)
    wake: WakeSettings = field(default_factory=WakeSettings)
    listen: ListenSettings = field(default_factory=ListenSettings)
    stt: SttSettings = field(default_factory=SttSettings)
    tts: TtsSettings = field(default_factory=TtsSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    source: Path | None = None
    token_cache: Path = field(default_factory=lambda: config_dir({}) / "desktop-token.json")


_SECTIONS: dict[str, type[Any]] = {
    "backend": BackendSettings,
    "wake": WakeSettings,
    "listen": ListenSettings,
    "stt": SttSettings,
    "tts": TtsSettings,
    "ui": UiSettings,
}

ENV_VARS: dict[str, tuple[str, str]] = {
    "BACKEND_URL": ("backend", "url"),
    "SECRET": ("backend", "secret"),
    "TOKEN": ("backend", "token"),
    "WAKE_ENABLED": ("wake", "enabled"),
    "WAKE_PHRASE": ("wake", "phrase"),
    "WAKE_THRESHOLD": ("wake", "threshold"),
    "HOTKEY": ("wake", "hotkey"),
    "STT_MODEL": ("stt", "model"),
    "STT_COMPUTE_TYPE": ("stt", "compute_type"),
    "STT_DEVICE": ("stt", "device"),
    "STT_LANGUAGE": ("stt", "language"),
    "TTS_ENGINE": ("tts", "engine"),
    "TTS_VOICE": ("tts", "voice"),
    "TTS_MODEL_DIR": ("tts", "model_dir"),
    "CHIME": ("ui", "chime"),
    "COLOR": ("ui", "color"),
}

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path("~/.config").expanduser())
    return Path(base) / "ceo-assistant"


def default_config_path(env: Mapping[str, str]) -> Path:
    return config_dir(env) / "desktop.toml"


def _coerce(value: Any, target: Any, where: str) -> Any:
    if target is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in (*_TRUE, *_FALSE):
            return value.strip().lower() in _TRUE
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    if target is int:
        if isinstance(value, bool):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: expected an integer, got {value!r}") from None
    if target is float:
        if isinstance(value, bool):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: expected a number, got {value!r}") from None
    if target is Path:
        if not isinstance(value, str | Path):
            raise ConfigError(f"{where}: expected a path, got {value!r}")
        return Path(value).expanduser()
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string, got {value!r}")
    return value


def _build_section(name: str, values: Mapping[str, Any]) -> Any:
    cls = _SECTIONS[name]
    hints = get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ConfigError(f"[{name}]: unknown setting(s): {', '.join(unknown)}")
    kwargs = {key: _coerce(value, hints[key], f"{name}.{key}") for key, value in values.items()}
    return cls(**kwargs)


def _validate(config: DesktopConfig) -> None:
    url = config.backend.url
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"backend.url must start with http:// or https://, got {url!r}")
    if not config.backend.client_name:
        raise ConfigError("backend.client_name must not be empty")
    if not 0.0 < config.wake.threshold <= 1.0:
        raise ConfigError("wake.threshold must be in (0, 1]")
    if config.wake.enabled and not config.wake.phrase.strip():
        raise ConfigError("wake.phrase must not be empty while the wake word is enabled")
    try:
        parse_hotkey(config.wake.hotkey)
    except HotkeyError as exc:
        raise ConfigError(f"wake.hotkey: {exc}") from None
    positive = {
        "wake.cooldown_seconds": config.wake.cooldown_seconds,
        "listen.silence_seconds": config.listen.silence_seconds,
        "listen.max_seconds": config.listen.max_seconds,
        "listen.start_timeout_seconds": config.listen.start_timeout_seconds,
        "backend.request_timeout_seconds": config.backend.request_timeout_seconds,
        "backend.reply_timeout_seconds": config.backend.reply_timeout_seconds,
        "tts.speed": config.tts.speed,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ConfigError(f"{name} must be greater than zero")
    if config.stt.beam_size < 1:
        raise ConfigError("stt.beam_size must be at least 1")
    if config.stt.compute_type not in COMPUTE_TYPES:
        raise ConfigError(
            f"stt.compute_type must be one of {', '.join(COMPUTE_TYPES)}, "
            f"got {config.stt.compute_type!r}"
        )
    if config.tts.engine not in TTS_ENGINES:
        raise ConfigError(f"tts.engine must be one of {', '.join(TTS_ENGINES)}")
    if config.ui.color not in COLOR_MODES:
        raise ConfigError(f"ui.color must be one of {', '.join(COLOR_MODES)}")


def load_config(
    path: Path | str | None = None, env: Mapping[str, str] | None = None
) -> DesktopConfig:
    """Load the configuration; an explicitly named file must exist."""
    env = os.environ if env is None else env
    explicit = path is not None or bool(env.get(CONFIG_ENV))
    config_path = Path(path or env.get(CONFIG_ENV) or default_config_path(env)).expanduser()

    raw: dict[str, Any] = {}
    source: Path | None = None
    if config_path.is_file():
        try:
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path}: invalid TOML: {exc}") from None
        source = config_path
    elif explicit:
        raise ConfigError(f"config file not found: {config_path}")

    unknown = sorted(set(raw) - set(_SECTIONS))
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    merged: dict[str, dict[str, Any]] = {}
    for name in _SECTIONS:
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] must be a table")
        merged[name] = dict(section)

    for suffix, (section_name, key) in ENV_VARS.items():
        value = env.get(ENV_PREFIX + suffix)
        if value is not None and value != "":
            merged[section_name][key] = value

    sections = {name: _build_section(name, values) for name, values in merged.items()}
    cache = env.get(TOKEN_CACHE_ENV)
    token_cache = Path(cache).expanduser() if cache else config_dir(env) / "desktop-token.json"
    config = DesktopConfig(**sections, source=source, token_cache=token_cache)
    _validate(config)
    return config


def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    return "set (" + "*" * min(len(value), 8) + ")"


def describe(config: DesktopConfig) -> list[tuple[str, str]]:
    """Human-readable settings with secrets masked, for ``--check``."""
    return [
        ("config file", str(config.source) if config.source else "(defaults, no file)"),
        ("backend", config.backend.url),
        ("shared secret", _mask(config.backend.secret)),
        ("access token", _mask(config.backend.token)),
        ("token cache", str(config.token_cache)),
        (
            "wake word",
            f"{config.wake.phrase!r} (threshold {config.wake.threshold})"
            if config.wake.enabled
            else "disabled",
        ),
        ("push-to-talk", config.wake.hotkey),
        (
            "speech-to-text",
            f"faster-whisper {config.stt.model} ({config.stt.compute_type}, {config.stt.device}, "
            f"language {config.stt.language_code or 'auto'})",
        ),
        ("text-to-speech", f"{config.tts.engine} voice {config.tts.effective_voice}"),
        ("chime", "on" if config.ui.chime else "off"),
    ]
