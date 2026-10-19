from __future__ import annotations

from pathlib import Path

import pytest

from assistant_desktop.config import (
    DEFAULT_BACKEND_URL,
    ConfigError,
    DesktopConfig,
    default_config_path,
    describe,
    load_config,
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_file(clean_env: dict[str, str]) -> None:
    config = load_config(env=clean_env)
    assert config.source is None
    assert config.backend.url == DEFAULT_BACKEND_URL
    assert config.wake.phrase == "atlas" and config.wake.engine == "auto"
    assert config.wake.hotkey == "ctrl+alt+space"
    assert config.stt.model == "small"
    assert config.stt.compute_type == "int8"
    assert config.stt.language_code == "en"
    assert config.tts.engine == "piper"
    assert config.tts.effective_voice == "en_US-lessac-medium"
    assert config.ui.chime is True
    assert config.token_cache == Path(clean_env["XDG_CONFIG_HOME"]) / "ceo-assistant" / (
        "desktop-token.json"
    )


def test_default_path_uses_xdg(clean_env: dict[str, str]) -> None:
    assert default_config_path(clean_env).name == "desktop.toml"
    assert default_config_path(clean_env).parent.name == "ceo-assistant"


def test_default_dataclass_is_constructible() -> None:
    assert DesktopConfig().backend.client_name == "desktop"


def test_file_at_default_location_is_loaded(clean_env: dict[str, str]) -> None:
    path = write(
        default_config_path(clean_env),
        """
[backend]
url = "https://assistant.example.com"
secret = "s3cret"

[wake]
phrase = "alexa"
threshold = 0.7
hotkey = "cmd+shift+j"

[stt]
model = "base.en"
compute_type = "float32"
language = "auto"

[tts]
engine = "kokoro"
model_dir = "~/voices"

[ui]
chime = false
""",
    )
    config = load_config(env=clean_env)
    assert config.source == path
    assert config.backend.url == "https://assistant.example.com"
    assert config.backend.secret == "s3cret"
    assert config.wake.phrase == "alexa"
    assert config.wake.threshold == 0.7
    assert config.stt.language_code is None
    assert config.tts.effective_voice == "af_heart"
    assert config.tts.model_dir == Path("~/voices").expanduser()
    assert config.ui.chime is False


def test_env_overrides_file(tmp_path: Path, clean_env: dict[str, str]) -> None:
    path = write(tmp_path / "c.toml", '[backend]\nurl = "http://file:1"\n[ui]\nchime = true\n')
    env = {
        **clean_env,
        "CEO_DESKTOP_CONFIG": str(path),
        "CEO_DESKTOP_BACKEND_URL": "http://env:2",
        "CEO_DESKTOP_SECRET": "from-env",
        "CEO_DESKTOP_CHIME": "off",
        "CEO_DESKTOP_WAKE_THRESHOLD": "0.25",
        "CEO_DESKTOP_HOTKEY": "ctrl+shift+f9",
        "CEO_DESKTOP_STT_MODEL": "medium",
        "CEO_DESKTOP_TTS_VOICE": "en_GB-alan-low",
        "CEO_DESKTOP_TOKEN_CACHE": str(tmp_path / "tok.json"),
        "CEO_DESKTOP_TOKEN": "",  # empty values are ignored
    }
    config = load_config(env=env)
    assert config.backend.url == "http://env:2"
    assert config.backend.secret == "from-env"
    assert config.backend.token == ""
    assert config.ui.chime is False
    assert config.wake.threshold == 0.25
    assert config.wake.hotkey == "ctrl+shift+f9"
    assert config.stt.model == "medium"
    assert config.tts.effective_voice == "en_GB-alan-low"
    assert config.token_cache == tmp_path / "tok.json"


def test_explicit_missing_file_is_an_error(tmp_path: Path, clean_env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", env=clean_env)
    with pytest.raises(ConfigError, match="not found"):
        load_config(env={**clean_env, "CEO_DESKTOP_CONFIG": str(tmp_path / "nope.toml")})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[backend\n", "invalid TOML"),
        ("[bogus]\n", "unknown section"),
        ("backend = 3\n", "must be a table"),
        ("[backend]\nnope = 1\n", "unknown setting"),
        ('[backend]\nurl = "ftp://x"\n', "http:// or https://"),
        ('[backend]\nclient_name = ""\n', "client_name"),
        ("[wake]\nthreshold = 0\n", "threshold"),
        ("[wake]\nthreshold = true\n", "expected a number"),
        ('[wake]\nthreshold = "high"\n', "expected a number"),
        ('[wake]\nphrase = " "\n', "phrase"),
        ('[wake]\nhotkey = "ctrl+"\n', "hotkey"),
        ('[wake]\nenabled = "maybe"\n', "true or false"),
        ("[wake]\ncooldown_seconds = -1\n", "greater than zero"),
        ("[stt]\nbeam_size = 0\n", "beam_size"),
        ("[stt]\nbeam_size = true\n", "integer"),
        ('[stt]\nbeam_size = "x"\n', "integer"),
        ('[stt]\ncompute_type = "int3"\n', "compute_type"),
        ("[stt]\nmodel = 5\n", "expected a string"),
        ('[tts]\nengine = "espeak"\n', "tts.engine"),
        ("[tts]\nmodel_dir = 3\n", "expected a path"),
        ('[ui]\ncolor = "rainbow"\n', "ui.color"),
    ],
)
def test_invalid_config(tmp_path: Path, clean_env: dict[str, str], text: str, message: str) -> None:
    path = write(tmp_path / "bad.toml", text)
    with pytest.raises(ConfigError, match=message):
        load_config(path, env=clean_env)


def test_wake_phrase_may_be_blank_when_disabled(tmp_path: Path, clean_env: dict[str, str]) -> None:
    path = write(tmp_path / "c.toml", '[wake]\nenabled = false\nphrase = ""\n')
    assert load_config(path, env=clean_env).wake.enabled is False


def test_describe_masks_secrets(clean_env: dict[str, str]) -> None:
    env = {**clean_env, "CEO_DESKTOP_SECRET": "supersecretvalue", "CEO_DESKTOP_WAKE_ENABLED": "no"}
    rows = dict(describe(load_config(env=env)))
    assert "supersecretvalue" not in rows["shared secret"]
    assert rows["shared secret"].startswith("set (")
    assert rows["access token"] == "(not set)"
    assert rows["wake word"] == "disabled"
    assert rows["config file"].startswith("(defaults")
    assert "faster-whisper small" in rows["speech-to-text"]
