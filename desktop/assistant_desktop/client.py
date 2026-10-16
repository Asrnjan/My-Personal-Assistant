"""Backend connection: a reconnecting WebSocket with a REST fallback.

Protocol on ``ws(s)://<backend>/ws?token=<token>``:

* server ``{"type": "event", "kind": "brief"|"alert"|"notice", "payload": {"text": ...}}``
  is handed to ``on_event`` (the voice loop chimes, then speaks the text);
* client ``{"type": "converse", "text", "conversation_id"}`` is answered by
  ``{"type": "reply", "conversation_id", "reply", "pending_actions", "needs_confirmation"}``;
* server ``{"type": "ping"}`` is answered with ``{"type": "pong"}``.

Reconnects use capped exponential backoff with jitter; the delay resets once a
connection is established and survives its first read. **Frames are dropped, never queued, while the
socket is down**: a ``converse`` goes over ``POST /converse`` instead, and a
queued pong or stale request would be meaningless after a reconnect. If the
socket drops while a reply is outstanding the turn fails rather than being
resent, because the backend may already have acted on it.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from assistant_desktop.auth import AuthError, BackendError, TokenProvider

log = logging.getLogger(__name__)

AUTH_CLOSE_CODES = (1008, 4401, 4403)


class ConnectionClosedError(Exception):
    """The WebSocket closed or broke."""


class WebSocketConnection(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...


Connector = Callable[[str], WebSocketConnection]


@dataclass(frozen=True)
class Reply:
    text: str
    conversation_id: str | None
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    needs_confirmation: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Reply:
        text = data.get("reply", data.get("text", ""))
        conversation_id = data.get("conversation_id")
        actions = data.get("pending_actions") or []
        return cls(
            text=str(text or ""),
            conversation_id=str(conversation_id) if conversation_id else None,
            pending_actions=[a for a in actions if isinstance(a, dict)],
            needs_confirmation=bool(data.get("needs_confirmation", False)),
            raw=data,
        )


@dataclass(frozen=True)
class PushEvent:
    kind: str
    text: str
    payload: dict[str, Any]


class Backoff:
    """Exponential backoff with proportional jitter, capped at ``cap`` seconds."""

    def __init__(
        self,
        base: float = 1.0,
        factor: float = 2.0,
        cap: float = 60.0,
        jitter: float = 0.2,
        rng: random.Random | None = None,
    ) -> None:
        if base <= 0 or factor < 1 or cap < base or not 0 <= jitter < 1:
            raise ValueError("invalid backoff parameters")
        self.base = base
        self.factor = factor
        self.cap = cap
        self.jitter = jitter
        # Jitter only spreads reconnects out; it is not security sensitive.
        self._rng = rng or random.Random()  # noqa: S311
        self.attempt = 0

    def next_delay(self) -> float:
        raw = min(self.cap, self.base * self.factor**self.attempt)
        self.attempt += 1
        spread = raw * self.jitter
        return min(self.cap, max(0.0, raw + self._rng.uniform(-spread, spread)))

    def reset(self) -> None:
        self.attempt = 0


class _WebsocketsConnection:
    """Adapts a ``websockets`` sync connection to :class:`WebSocketConnection`."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def send(self, message: str) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            self._conn.send(message)
        except ConnectionClosed as exc:
            raise ConnectionClosedError(str(exc)) from exc

    def recv(self, timeout: float | None = None) -> str:
        from websockets.exceptions import ConnectionClosed

        try:
            data = self._conn.recv(timeout=timeout)
        except ConnectionClosed as exc:
            code = getattr(exc.rcvd, "code", None)
            if code in AUTH_CLOSE_CODES:
                raise AuthError(f"backend closed the socket (code {code})") from exc
            raise ConnectionClosedError(str(exc)) from exc
        return data if isinstance(data, str) else bytes(data).decode("utf-8")

    def close(self) -> None:
        self._conn.close()


def websockets_connector(url: str, *, open_timeout: float = 10.0) -> WebSocketConnection:
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    try:
        conn = connect(url, open_timeout=open_timeout, max_size=4 * 1024 * 1024)
    except InvalidStatus as exc:
        status = exc.response.status_code
        if status in (401, 403):
            raise AuthError(f"backend refused the WebSocket (HTTP {status})") from exc
        raise ConnectionClosedError(f"WebSocket handshake failed: HTTP {status}") from exc
    return _WebsocketsConnection(conn)


class _PendingReply:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.reply: Reply | None = None
        self.error: str | None = None


def ws_url(base_url: str, token: str) -> str:
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/ws?token={quote(token, safe='')}"


StatusCallback = Callable[[bool, str], None]


class BackendClient:
    """Talks to the backend; run :meth:`run` (or :meth:`start`) for push events."""

    def __init__(
        self,
        base_url: str,
        tokens: TokenProvider,
        http: httpx.Client,
        *,
        connector: Connector = websockets_connector,
        backoff: Backoff | None = None,
        sleep: Callable[[float], None] | None = None,
        reply_timeout: float = 60.0,
        recv_timeout: float = 1.0,
        on_event: Callable[[PushEvent], None] | None = None,
        on_status: StatusCallback | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._tokens = tokens
        self._http = http
        self._connector = connector
        self.backoff = backoff or Backoff()
        self._stop = threading.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._reply_timeout = reply_timeout
        self._recv_timeout = recv_timeout
        self.on_event = on_event
        self.on_status = on_status
        self.conversation_id: str | None = None
        self._conn: WebSocketConnection | None = None
        self._send_lock = threading.Lock()
        self._converse_lock = threading.Lock()
        self._pending: _PendingReply | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def start(self) -> threading.Thread:
        self._stop.clear()
        thread = threading.Thread(target=self.run, name="backend-ws", daemon=True)
        thread.start()
        self._thread = thread
        return thread

    def stop(self) -> None:
        self._stop.set()
        conn = self._conn
        if conn is not None:
            self._close_quietly(conn)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _status(self, connected: bool, detail: str) -> None:
        if self.on_status is not None:
            self.on_status(connected, detail)

    @staticmethod
    def _close_quietly(conn: WebSocketConnection) -> None:
        try:
            conn.close()
        except Exception as exc:
            log.debug("error while closing the WebSocket: %s", exc)

    def _open(self) -> WebSocketConnection:
        """Connect once, refreshing the token a single time if it was rejected."""
        try:
            return self._connector(ws_url(self.base_url, self._tokens.get()))
        except AuthError:
            if not self._tokens.invalidate():
                raise
            log.info("access token rejected; exchanging the shared secret again")
            return self._connector(ws_url(self.base_url, self._tokens.get()))

    def run(self) -> None:
        """Connect, read, and reconnect until :meth:`stop` is called."""
        while not self._stop.is_set():
            try:
                conn = self._open()
            except Exception as exc:
                delay = self.backoff.next_delay()
                log.info("WebSocket connect failed (%s); retrying in %.1fs", exc, delay)
                self._status(False, f"offline, retrying in {delay:.0f}s")
                self._sleep(delay)
                continue
            self._conn = conn
            self._status(True, "connected")
            try:
                self._read_loop(conn)
            except AuthError as exc:
                # e.g. close code 1008 once the token is revoked: fetch a new one.
                log.info("WebSocket closed by the backend: %s", exc)
                self._tokens.invalidate()
            except Exception as exc:
                log.info("WebSocket closed: %s", exc)
            finally:
                self._conn = None
                self._close_quietly(conn)
                self._fail_pending("connection lost before the reply arrived")
            if self._stop.is_set():
                break
            delay = self.backoff.next_delay()
            self._status(False, f"reconnecting in {delay:.0f}s")
            self._sleep(delay)
        self._status(False, "stopped")

    def _read_loop(self, conn: WebSocketConnection) -> None:
        # The backoff resets once the connection survives its first read, so a
        # server that accepts and immediately closes still gets growing delays.
        healthy = False
        while not self._stop.is_set():
            try:
                message = conn.recv(timeout=self._recv_timeout)
            except TimeoutError:
                message = None
            if not healthy:
                healthy = True
                self.backoff.reset()
            if message is not None:
                self.handle_message(message)

    # -- incoming ----------------------------------------------------------

    def handle_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except ValueError:
            log.warning("ignoring non-JSON frame from the backend")
            return
        if not isinstance(data, dict):
            log.warning("ignoring unexpected frame from the backend")
            return
        kind = data.get("type")
        if kind == "ping":
            self.send_json({"type": "pong"})
        elif kind == "pong":
            log.debug("keepalive pong")
        elif kind == "event":
            self._handle_event(data)
        elif kind == "reply":
            self._handle_reply(data)
        elif kind == "error":
            pending = self._pending
            if pending is not None:
                pending.error = str(data.get("message") or data.get("detail") or "backend error")
                pending.done.set()
            else:
                log.warning("backend error: %s", data)
        else:
            log.debug("ignoring frame of type %r", kind)

    def _handle_event(self, data: dict[str, Any]) -> None:
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            log.debug("event %r without text ignored", data.get("kind"))
            return
        event = PushEvent(kind=str(data.get("kind") or "notice"), text=text, payload=payload)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                log.exception("event handler failed")

    def _handle_reply(self, data: dict[str, Any]) -> None:
        reply = Reply.from_json(data)
        if reply.conversation_id:
            self.conversation_id = reply.conversation_id
        pending = self._pending
        if pending is None:
            log.debug("unsolicited reply ignored")
            return
        pending.reply = reply
        pending.done.set()

    def _fail_pending(self, reason: str) -> None:
        pending = self._pending
        if pending is not None and not pending.done.is_set():
            pending.error = reason
            pending.done.set()

    # -- outgoing ----------------------------------------------------------

    def send_json(self, payload: dict[str, Any]) -> bool:
        """Send a frame now; returns False (frame dropped) when disconnected."""
        conn = self._conn
        if conn is None:
            log.debug("dropping %s frame while disconnected", payload.get("type"))
            return False
        with self._send_lock:
            conn.send(json.dumps(payload))
        return True

    def converse(self, text: str) -> Reply:
        """One conversation turn, over the WebSocket when up, else over REST."""
        with self._converse_lock:
            if self._conn is not None:
                pending = _PendingReply()
                self._pending = pending
                try:
                    sent = self.send_json(
                        {"type": "converse", "text": text, "conversation_id": self.conversation_id}
                    )
                except Exception as exc:
                    log.info("WebSocket send failed (%s); using REST", exc)
                    sent = False
                try:
                    if sent:
                        if not pending.done.wait(self._reply_timeout):
                            raise BackendError("the assistant took too long to reply")
                        if pending.reply is None:
                            raise BackendError(pending.error or "no reply")
                        return pending.reply
                finally:
                    self._pending = None
            return self.converse_rest(text)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.get()}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = self.base_url + path
        try:
            response = self._http.request(method, url, headers=self._auth_headers(), **kwargs)
            if response.status_code == httpx.codes.UNAUTHORIZED and self._tokens.invalidate():
                response = self._http.request(method, url, headers=self._auth_headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise BackendError(f"cannot reach the backend at {self.base_url}: {exc}") from exc
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise AuthError("the backend rejected the access token")
        if response.is_error:
            raise BackendError(f"{method} {path} failed: HTTP {response.status_code}")
        return response

    def converse_rest(self, text: str) -> Reply:
        response = self._request(
            "POST", "/converse", json={"text": text, "conversation_id": self.conversation_id}
        )
        try:
            data = response.json()
        except ValueError:
            raise BackendError("POST /converse returned invalid JSON") from None
        if not isinstance(data, dict):
            raise BackendError("POST /converse returned an unexpected body")
        reply = Reply.from_json(data)
        if reply.conversation_id:
            self.conversation_id = reply.conversation_id
        return reply

    def health(self) -> dict[str, Any]:
        try:
            response = self._http.get(self.base_url + "/health")
        except httpx.HTTPError as exc:
            raise BackendError(f"cannot reach the backend at {self.base_url}: {exc}") from exc
        if response.is_error:
            raise BackendError(f"GET /health failed: HTTP {response.status_code}")
        data = response.json()
        return data if isinstance(data, dict) else {}

    def whoami(self) -> str:
        data = self._request("GET", "/auth/whoami").json()
        return str(data.get("client_name", "")) if isinstance(data, dict) else ""
