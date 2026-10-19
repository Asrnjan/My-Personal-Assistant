# Desktop voice client

`assistant-desktop` is the voice front end for the Chief of Staff backend. Say
**"Atlas"** (or "Atlas, brief me" in one go) or press **Ctrl+Alt+Space**, speak,
and hear the answer. When
the backend pushes a morning brief or an alert, the client plays a soft chime
and reads it out.

Everything runs locally and is free/open source:

| Part | Library | Notes |
|---|---|---|
| Wake word | faster-whisper, or [openWakeWord](https://github.com/dscripka/openWakeWord) | default `atlas`: any name works; openWakeWord for its own models |
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | default model `small`, `int8` on CPU, Silero VAD |
| Text-to-speech | [Piper](https://github.com/OHF-Voice/piper1-gpl) (default) or [Kokoro](https://github.com/hexgrad/kokoro) | voice configurable |
| Audio | [sounddevice](https://python-sounddevice.readthedocs.io/) (PortAudio) | |
| Hotkey | [pynput](https://github.com/moses-palmer/pynput) | push-to-talk and barge-in |
| Tray | [pystray](https://github.com/moses-palmer/pystray) + Pillow | Windows and macOS |

The core package needs only `httpx`, `websockets` and `numpy`. The heavy
libraries are optional extras and are imported only when used:

- `voice`: wake word, speech-to-text, Piper, audio I/O, global hotkey
- `kokoro`: the Kokoro TTS engine (pulls in PyTorch)
- `tray`: the system tray icon

Without `voice` the client still works as a typed chat in the terminal.

## Install

With [pipx](https://pipx.pypa.io/):

```bash
pipx install "./desktop[voice,tray]"      # from the repository root
```

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "./desktop[voice,tray]"
```

On Linux, install PortAudio first (`sudo apt install libportaudio2`). On macOS,
grant the terminal (or the built app) **Microphone** and **Accessibility**
permission; the global hotkey needs the latter.

Download a Piper voice once (about 60 MB):

```bash
python -m piper.download_voices --download-dir ~/.local/share/ceo-assistant/voices en_US-lessac-medium
```

faster-whisper downloads the Whisper model and openWakeWord its wake word model
on first use; both are cached afterwards.

## Configure

Copy [`desktop.example.toml`](desktop.example.toml) to
`~/.config/ceo-assistant/desktop.toml` (or set `CEO_DESKTOP_CONFIG` to another
path). Every setting is optional and also has an environment variable; the
example file lists them all. The ones you usually need:

| Setting | Env var | Default |
|---|---|---|
| `backend.url` | `CEO_DESKTOP_BACKEND_URL` | `http://127.0.0.1:8000` |
| `backend.secret` | `CEO_DESKTOP_SECRET` | the backend's `API_AUTH_TOKEN` |
| `wake.phrase` | `CEO_DESKTOP_WAKE_PHRASE` | `atlas` |
| `wake.engine` | `CEO_DESKTOP_WAKE_ENGINE` | `auto` (`whisper` for a name, `openwakeword` for its models) |
| `wake.hotkey` | `CEO_DESKTOP_HOTKEY` | `ctrl+alt+space` |
| `stt.model` / `stt.compute_type` | `CEO_DESKTOP_STT_MODEL` / `..._COMPUTE_TYPE` | `small` / `int8` |
| `tts.engine` / `tts.voice` | `CEO_DESKTOP_TTS_ENGINE` / `..._VOICE` | `piper` / `en_US-lessac-medium` |
| `ui.chime` | `CEO_DESKTOP_CHIME` | `true` |

On first run the client trades the shared secret for its own token
(`POST /auth/token`, `client_name = "desktop"`) and caches it in
`~/.config/ceo-assistant/desktop-token.json` (mode 0600). If the backend later
rejects the token, the client exchanges the secret again automatically.

## Run

```bash
assistant-desktop --check          # show the config and test the backend and components
assistant-desktop                  # tray on Windows/macOS, terminal mode on Linux
assistant-desktop --cli            # terminal mode anywhere
assistant-desktop --tray           # force the tray icon
assistant-desktop --once "What's on my calendar today?"   # one request, print the reply
assistant-desktop --once "Brief me" --speak               # ... and speak it
assistant-desktop --cli --text-only                       # no microphone, typed input only
```

In terminal mode press **Enter** for push-to-talk (useful under Wayland, where
global hotkeys are blocked), type a request and press Enter to send it as text,
or `/quit` to leave. The status line shows a neon orb whose colour follows the
state: cyan idle, violet listening, amber thinking, magenta speaking. Output
falls back to plain lines when it is not a terminal or `NO_COLOR` is set.

The tray icon is the same glowing orb, pulsing while it listens, thinks or
speaks. Its menu has **Talk now**, **Chime before briefs** and **Quit**.

### How a turn works

```
IDLE --wake word / hotkey--> LISTENING --end of speech--> THINKING --reply--> SPEAKING --> IDLE
```

- End of speech is detected from the microphone level against the room's noise
  floor (learnt while idle): 0.8 s of silence ends the utterance; nothing said
  within 6 s returns to idle.
- Pressing the hotkey while the assistant speaks stops it and listens again
  (barge-in).
- Any error returns to idle and is shown and spoken.
- A wake word right after the assistant finished speaking is ignored, so its own
  voice cannot re-trigger it.

### Backend connection

The client keeps a WebSocket open to `/ws?token=...` for pushed events and
conversation, answers the backend's `ping` with `pong`, and reconnects with
exponential backoff (1 s doubling up to 60 s, ±20% jitter, reset once a
connection is up and survives its first read; a server that accepts and
immediately closes keeps backing off). A socket closed for a bad token (code
1008) makes the client exchange the shared secret again. While the socket is down, nothing is queued:
conversation turns go over `POST /converse` instead. If the socket drops while a
reply is outstanding, that turn reports an error rather than being resent,
because the backend may already have acted on it.

## Build a standalone app (optional)

```bash
cd desktop
uv venv .build && uv pip install --python .build -e ".[voice,tray]" pyinstaller
.build/bin/python scripts/build.py            # or: --onefile
```

The app lands in `desktop/dist/assistant-desktop/`. Models are not bundled; they
download on first run as above.

## Develop

```bash
cd desktop
uv venv .venv && uv pip install --python .venv -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest          # coverage gate: 85%
```

The tests need no models, microphone or speakers: the heavy libraries are
replaced by fakes, and audio comes from the short WAV files in
`tests/fixtures/` (regenerate with `python tests/fixtures/make_fixtures.py`).

## Licences to know about

All components are free and open source. Two caveats: Piper 1.3+ is GPL-3.0
(fine for personal use; the client only calls it as a library), and
openWakeWord's pretrained wake word models are licensed CC BY-NC-SA 4.0
(non-commercial). The default name (`atlas`) doesn't use them: it is heard by
Whisper. For an openWakeWord model in commercial use, train your own and set
`wake.phrase` to its `.onnx` path.
