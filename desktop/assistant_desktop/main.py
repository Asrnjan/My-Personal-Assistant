"""``assistant-desktop``: tray app (Windows/macOS), CLI mode, one-shot and checks."""

from __future__ import annotations

import argparse
import importlib.util
import logging
import queue
import signal
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TextIO

import httpx

from assistant_desktop import __version__
from assistant_desktop.audio import (
    AudioUnavailableError,
    Int16Array,
    MicrophoneStream,
    SoundDevicePlayer,
    generate_chime,
    import_sounddevice,
)
from assistant_desktop.auth import AuthError, BackendError, TokenProvider, TokenStore
from assistant_desktop.client import BackendClient, Connector, PushEvent, websockets_connector
from assistant_desktop.config import ConfigError, DesktopConfig, describe, load_config
from assistant_desktop.hotkey import GlobalHotkey
from assistant_desktop.stt import (
    EnergyEndpointer,
    NoiseFloor,
    QueueRecorder,
    SttUnavailableError,
    WhisperTranscriber,
)
from assistant_desktop.tts import (
    TextSpeaker,
    TtsUnavailableError,
    VoiceSpeaker,
    create_synthesizer,
)
from assistant_desktop.ui import StatusLine
from assistant_desktop.wakeword import (
    AudioRouter,
    OpenWakeWordDetector,
    Speaker,
    State,
    Trigger,
    WakeWordHandler,
)

log = logging.getLogger("assistant_desktop")

OPTIONAL_COMPONENTS = (
    ("wake word", "openwakeword", "voice"),
    ("speech-to-text", "faster_whisper", "voice"),
    ("Piper TTS", "piper", "voice"),
    ("audio I/O", "sounddevice", "voice"),
    ("global hotkey", "pynput", "voice"),
    ("Kokoro TTS", "kokoro", "kokoro"),
    ("tray icon", "pystray", "tray"),
)


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def event_role(kind: str) -> str:
    """Scheduled briefs arrive as ``brief``, ``morning`` or ``evening``."""
    if kind in ("brief", "morning", "evening"):
        return "brief"
    return kind if kind == "alert" else "notice"


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, AuthError):
        return "I'm not authorised with the backend. Check the shared secret."
    if isinstance(exc, BackendError):
        return "I can't reach the assistant backend right now."
    if isinstance(exc, AudioUnavailableError):
        return "The microphone or speaker isn't available."
    if isinstance(exc, SttUnavailableError | TtsUnavailableError):
        return str(exc)
    return f"Something went wrong: {exc}" if str(exc) else "Something went wrong."


def build_speaker(config: DesktopConfig) -> Speaker:
    """Voice output when the TTS engine and audio stack exist, else text only."""
    engine_module = "kokoro" if config.tts.engine == "kokoro" else "piper"
    if not module_available(engine_module):
        log.warning("%s is not installed; replies are shown as text only", engine_module)
        return TextSpeaker()
    try:
        import_sounddevice()
    except AudioUnavailableError as exc:
        log.warning("%s; replies are shown as text only", exc)
        return TextSpeaker()
    chime = generate_chime() if config.ui.chime else None
    speaker: Speaker = VoiceSpeaker(
        create_synthesizer(config.tts), SoundDevicePlayer(), chime=chime
    )
    return speaker


@dataclass
class VoiceInput:
    """Microphone, end-of-speech recorder, local STT and optional wake word."""

    mic: MicrophoneStream
    listen_queue: queue.Queue[Int16Array]
    recorder: QueueRecorder
    transcriber: WhisperTranscriber
    detector: OpenWakeWordDetector | None
    noise: NoiseFloor


def build_voice_input(config: DesktopConfig) -> VoiceInput | None:
    """``None`` (typed input only) when faster-whisper or sounddevice is missing."""
    if not module_available("faster_whisper"):
        log.warning("faster-whisper is not installed; voice input disabled")
        return None
    try:
        import_sounddevice()
    except AudioUnavailableError as exc:
        log.warning("%s; voice input disabled", exc)
        return None
    listen_queue: queue.Queue[Int16Array] = queue.Queue()
    mic = MicrophoneStream()
    noise = NoiseFloor()
    listen = config.listen

    def endpointer() -> EnergyEndpointer:
        return EnergyEndpointer(
            sample_rate=mic.sample_rate,
            threshold_dbfs=listen.energy_threshold_dbfs,
            silence_seconds=listen.silence_seconds,
            start_timeout_seconds=listen.start_timeout_seconds,
            max_seconds=listen.max_seconds,
            noise_floor_dbfs=noise.value,
        )

    detector = None
    if config.wake.enabled:
        if module_available("openwakeword"):
            detector = OpenWakeWordDetector(config.wake.phrase, threshold=config.wake.threshold)
        else:
            log.warning("openwakeword is not installed; use the push-to-talk hotkey")
    return VoiceInput(
        mic=mic,
        listen_queue=listen_queue,
        recorder=QueueRecorder(listen_queue, endpointer, sample_rate=mic.sample_rate),
        transcriber=WhisperTranscriber(
            config.stt.model,
            compute_type=config.stt.compute_type,
            device=config.stt.device,
            language=config.stt.language_code,
            vad_filter=config.stt.vad_filter,
            beam_size=config.stt.beam_size,
        ),
        detector=detector,
        noise=noise,
    )


class DesktopApp:
    """Wires the backend client, the voice loop and the UI together."""

    def __init__(
        self,
        config: DesktopConfig,
        client: BackendClient,
        *,
        speaker: Speaker,
        voice: VoiceInput | None = None,
        on_state: Callable[[State, str], None] | None = None,
        on_message: Callable[[str, str], None] | None = None,
        on_connection: Callable[[bool, str], None] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.voice = voice
        self.stop_requested = threading.Event()
        self.handler = WakeWordHandler(
            ask=self.ask,
            speaker=speaker,
            recorder=voice.recorder if voice else None,
            transcriber=voice.transcriber if voice else None,
            cooldown_seconds=config.wake.cooldown_seconds,
            chime=config.ui.chime,
            on_state=on_state,
            on_message=on_message,
            error_text=friendly_error,
        )
        client.on_event = self.on_event
        client.on_status = on_connection
        self._router: AudioRouter | None = None
        self._hotkey: GlobalHotkey | None = None
        self._threads: list[threading.Thread] = []

    def ask(self, text: str) -> str:
        reply = self.client.converse(text)
        return reply.text or "Done."

    def on_event(self, event: PushEvent) -> None:
        self.handler.announce(event.text, event_role(event.kind))

    def push_to_talk(self) -> bool:
        return self.handler.trigger(Trigger.HOTKEY)

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def start(self) -> None:
        self.handler.start()
        self.client.start()
        if self.voice is not None:
            self._start_voice(self.voice)

    def _start_voice(self, voice: VoiceInput) -> None:  # pragma: no cover - device glue
        voice.mic.start()
        self._router = AudioRouter(
            voice.mic, self.handler, voice.listen_queue, voice.detector, voice.noise
        )
        self._spawn(self._router.run, "audio-router")
        self._spawn(voice.transcriber.load, "stt-preload")
        self._hotkey = GlobalHotkey(self.config.wake.hotkey, self.push_to_talk)
        self._hotkey.start()

    def request_stop(self) -> None:
        self.stop_requested.set()

    def stop(self) -> None:
        self.stop_requested.set()
        if self._hotkey is not None:  # pragma: no cover - device glue
            self._hotkey.stop()
        if self._router is not None:  # pragma: no cover - device glue
            self._router.stop()
        self.handler.stop()
        self.client.stop()
        if self.voice is not None:  # pragma: no cover - device glue
            self.voice.mic.close()


HELP = "Enter = push-to-talk  ·  type a request and press Enter  ·  /help  ·  /quit (or Ctrl+C)"


def run_cli(app: DesktopApp, status: StatusLine, stdin: Iterable[str]) -> int:
    hints = []
    if app.voice is not None:
        if app.voice.detector is not None:
            hints.append(f"say '{app.config.wake.phrase}'")
        hints.append(f"press {app.config.wake.hotkey} or Enter")
    hints.append("type a request")
    status.banner(" · ".join(hints) + " · /quit to exit")
    status.set_state(State.IDLE, "ready")
    app.start()
    ticker_stop = threading.Event()
    if status.live:

        def tick() -> None:
            while not ticker_stop.wait(0.12):
                status.tick()

        threading.Thread(target=tick, name="status-ticker", daemon=True).start()
    try:
        for line in stdin:
            command = line.strip()
            if command in ("/quit", "/exit", "/q"):
                break
            if command == "/help":
                status.message("info", HELP)
            elif not command:
                if not app.push_to_talk() and app.voice is not None:
                    status.message("info", "Busy, or just triggered; try again in a moment.")
            elif not app.handler.submit_text(command):
                status.message("info", "Still working on the last request; one moment.")
        else:
            status.message("info", "Input closed; still listening. Press Ctrl+C to quit.")
            while not app.stop_requested.wait(0.5):
                continue
    except KeyboardInterrupt:
        status.message("info", "Shutting down.")
    finally:
        ticker_stop.set()
        app.stop()
        status.close()
    return 0


def run_tray(app: DesktopApp) -> int:  # pragma: no cover - needs a desktop session
    from assistant_desktop.tray import TrayApp

    def toggle_chime() -> None:
        app.handler.chime = not app.handler.chime

    tray = TrayApp(
        name=app.config.ui.name,
        on_talk=app.push_to_talk,
        on_quit=app.stop,
        chime=lambda: app.handler.chime,
        toggle_chime=toggle_chime,
    )
    app.handler.add_listener(tray.set_state)

    def notify(role: str, text: str) -> None:
        if role in ("brief", "alert", "error"):
            tray.notify(f"{app.config.ui.name} · {role}", text)

    app.handler.add_message_listener(notify)
    tray.run(setup=app.start)
    return 0


def run_once(
    app: DesktopApp, status: StatusLine, text: str, *, speak: bool, speaker: Speaker
) -> int:
    try:
        reply = app.client.converse(text)
    except (AuthError, BackendError) as exc:
        status.message("error", str(exc))
        return 1
    status.message("assistant", reply.text)
    if reply.needs_confirmation:
        status.message("info", "Awaiting your confirmation: reply 'yes' or 'cancel'.")
    if speak:
        try:
            speaker.speak(reply.text)
        except Exception as exc:
            status.message("error", f"could not speak the reply: {friendly_error(exc)}")
    return 0


def run_check(config: DesktopConfig, client: BackendClient, out: TextIO) -> int:
    def line(ok: bool | None, label: str, detail: str) -> None:
        mark = {True: "ok  ", False: "FAIL", None: "--  "}[ok]
        out.write(f"  [{mark}] {label:<16} {detail}\n")

    out.write(f"assistant-desktop {__version__}\n\nConfiguration\n")
    for key, value in describe(config):
        out.write(f"  {key:<22} {value}\n")
    out.write("\nBackend\n")
    healthy = authed = False
    try:
        health = client.health()
        healthy = health.get("status") == "ok"
        line(
            healthy,
            "health",
            f"status {health.get('status', '?')}, version {health.get('version', '?')}",
        )
    except BackendError as exc:
        line(False, "health", str(exc))
    try:
        name = client.whoami()
        authed = True
        line(True, "auth", f"token accepted (client {name or '?'})")
    except (AuthError, BackendError) as exc:
        line(False, "auth", str(exc))
    out.write("\nOptional components\n")
    for label, module, extra in OPTIONAL_COMPONENTS:
        present = module_available(module)
        line(True if present else None, label, "installed" if present else f"pip extra [{extra}]")
    out.flush()
    return 0 if healthy and authed else 1


def _terminate(_signum: int, _frame: object) -> None:
    """SIGTERM shuts down like Ctrl+C."""
    raise KeyboardInterrupt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant-desktop",
        description="Voice client for the Chief of Staff assistant.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cli", action="store_true", help="terminal mode (default on Linux)")
    mode.add_argument("--tray", action="store_true", help="system tray mode (Windows/macOS)")
    mode.add_argument("--once", metavar="TEXT", help="send one request, print the reply, exit")
    mode.add_argument("--check", action="store_true", help="validate config and connectivity")
    parser.add_argument("--config", metavar="PATH", help="config file (or CEO_DESKTOP_CONFIG)")
    parser.add_argument("--speak", action="store_true", help="with --once: also speak the reply")
    parser.add_argument("--text-only", action="store_true", help="no microphone; typed input only")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more logging (-vv)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def default_mode(platform: str = sys.platform) -> str:
    if platform in ("win32", "darwin"):
        from assistant_desktop.tray import tray_available

        if tray_available():
            return "tray"
    return "cli"


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    http_transport: httpx.BaseTransport | None = None,
    connector: Connector | None = None,
    stdin: Iterable[str] | None = None,
    stdout: TextIO | None = None,
    voice_factory: Callable[[DesktopConfig], VoiceInput | None] = build_voice_input,
    speaker_factory: Callable[[DesktopConfig], Speaker] = build_speaker,
) -> int:
    args = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    reconfigure = getattr(out, "reconfigure", None)
    if callable(reconfigure):
        # A reply may contain characters the console code page cannot show.
        reconfigure(errors="replace")
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)]
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config, env)
    except ConfigError as exc:
        out.write(f"configuration error: {exc}\n")
        return 2

    status = StatusLine(out, color=config.ui.color, name=config.ui.name)
    http_kwargs: dict[str, Any] = {"timeout": config.backend.request_timeout_seconds}
    if http_transport is not None:
        http_kwargs["transport"] = http_transport
    with httpx.Client(**http_kwargs) as http:
        tokens = TokenProvider(
            http,
            config.backend.url,
            secret=config.backend.secret,
            token=config.backend.token,
            client_name=config.backend.client_name,
            store=TokenStore(config.token_cache),
        )
        client = BackendClient(
            config.backend.url,
            tokens,
            http,
            connector=connector or websockets_connector,
            reply_timeout=config.backend.reply_timeout_seconds,
        )
        if args.check:
            return run_check(config, client, out)

        speaker = speaker_factory(config) if (args.speak or not args.once) else TextSpeaker()
        if args.once:
            app = DesktopApp(config, client, speaker=speaker)
            return run_once(app, status, args.once, speak=args.speak, speaker=speaker)

        mode = "cli" if args.cli else "tray" if args.tray else default_mode()
        voice = None if args.text_only else voice_factory(config)
        app = DesktopApp(
            config,
            client,
            speaker=speaker,
            voice=voice,
            on_state=status.set_state,
            on_message=status.message,
            on_connection=status.set_connection,
        )
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _terminate)
        if mode == "tray":  # pragma: no cover - needs a desktop session
            from assistant_desktop.tray import tray_available

            if not tray_available():
                out.write("tray mode needs the 'tray' extra (pystray, pillow)\n")
                return 2
            return run_tray(app)
        return run_cli(app, status, stdin if stdin is not None else sys.stdin)
