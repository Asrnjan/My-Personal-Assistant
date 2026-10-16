from __future__ import annotations

import json
import queue
import random
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from assistant_desktop.auth import AuthError, BackendError, TokenProvider
from assistant_desktop.client import (
    BackendClient,
    Backoff,
    ConnectionClosedError,
    PushEvent,
    Reply,
    WebSocketConnection,
    websockets_connector,
    ws_url,
)

BASE = "http://backend.test"


# -- fakes ------------------------------------------------------------------


class FakeConnection:
    """Scripted WebSocket: inbound frames from a queue, a responder for sends."""

    def __init__(
        self,
        frames: list[str] | None = None,
        *,
        responder: Callable[[dict[str, Any]], list[str] | None] | None = None,
        close_when_empty: bool = True,
    ) -> None:
        self.inbound: queue.Queue[str | Exception] = queue.Queue()
        for frame in frames or []:
            self.inbound.put(frame)
        self.close_when_empty = close_when_empty
        self.responder = responder
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.fail_send = False

    def send(self, message: str) -> None:
        if self.fail_send:
            raise ConnectionClosedError("send failed")
        payload = json.loads(message)
        self.sent.append(payload)
        if self.responder is not None:
            for frame in self.responder(payload) or []:
                self.inbound.put(frame)

    def recv(self, timeout: float | None = None) -> str:
        try:
            item = self.inbound.get(timeout=timeout if not self.close_when_empty else 0.001)
        except queue.Empty:
            if self.close_when_empty:
                raise ConnectionClosedError("server went away") from None
            raise TimeoutError from None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True
        self.inbound.put(ConnectionClosedError("closed locally"))


class FakeConnector:
    def __init__(self, outcomes: list[FakeConnection | Exception]) -> None:
        self.outcomes = outcomes
        self.urls: list[str] = []

    def __call__(self, url: str) -> WebSocketConnection:
        self.urls.append(url)
        if not self.outcomes:
            raise ConnectionRefusedError("no more scripted outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSleep:
    """Records delays; stops the client after ``limit`` sleeps."""

    def __init__(self, limit: int) -> None:
        self.delays: list[float] = []
        self.limit = limit
        self.client: BackendClient | None = None

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if len(self.delays) >= self.limit and self.client is not None:
            self.client.stop()


def rest_transport(
    log: list[httpx.Request], *, converse_status: int = 200, body: Any = None
) -> httpx.MockTransport:
    issued = iter(["tok-1", "tok-2", "tok-3"])

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": next(issued)})
        if request.url.path == "/converse":
            if converse_status != 200:
                return httpx.Response(converse_status, json={"detail": "x"})
            if body is not None:
                return httpx.Response(200, content=body)
            sent = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "conversation_id": sent["conversation_id"] or "conv-rest",
                    "reply": f"REST heard: {sent['text']}",
                    "pending_actions": [{"id": "a1"}, "junk"],
                    "needs_confirmation": True,
                },
            )
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "0.1.0"})
        if request.url.path == "/auth/whoami":
            if request.headers["authorization"] != "Bearer tok-1":
                return httpx.Response(401)
            return httpx.Response(200, json={"client_name": "desktop"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def make_client(
    connector: Callable[[str], WebSocketConnection] | None = None,
    *,
    transport: httpx.MockTransport | None = None,
    sleep: FakeSleep | None = None,
    secret: str = "s",
    token: str = "",
    **kwargs: Any,
) -> BackendClient:
    http = httpx.Client(transport=transport or rest_transport([]))
    tokens = TokenProvider(http, BASE, secret=secret, token=token)
    client = BackendClient(
        BASE,
        tokens,
        http,
        connector=connector or FakeConnector([]),
        backoff=Backoff(base=1, factor=2, cap=8, jitter=0),
        sleep=sleep,
        recv_timeout=0.01,
        **kwargs,
    )
    if sleep is not None:
        sleep.client = client
    return client


# -- backoff ----------------------------------------------------------------


def test_backoff_grows_and_caps() -> None:
    backoff = Backoff(base=1, factor=2, cap=10, jitter=0)
    assert [backoff.next_delay() for _ in range(6)] == [1, 2, 4, 8, 10, 10]
    backoff.reset()
    assert backoff.next_delay() == 1


def test_backoff_jitter_stays_in_bounds() -> None:
    backoff = Backoff(base=4, factor=1, cap=60, jitter=0.25, rng=random.Random(7))
    delays = [backoff.next_delay() for _ in range(200)]
    assert all(3.0 <= d <= 5.0 for d in delays)
    assert len(set(delays)) > 100  # actually jittered
    capped = Backoff(base=60, factor=2, cap=60, jitter=0.5, rng=random.Random(1))
    assert all(capped.next_delay() <= 60 for _ in range(50))


@pytest.mark.parametrize(
    "kwargs", [{"base": 0}, {"factor": 0.5}, {"base": 10, "cap": 5}, {"jitter": 1.0}]
)
def test_backoff_rejects_bad_parameters(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        Backoff(**kwargs)


# -- reconnect loop ---------------------------------------------------------


def test_reconnect_backoff_resets_after_success() -> None:
    connection = FakeConnection(['{"type": "ping"}'])
    connector = FakeConnector(
        [
            ConnectionRefusedError("down"),
            OSError("still down"),
            TimeoutError("slow"),
            connection,
            ConnectionRefusedError("down again"),
        ]
    )
    sleep = FakeSleep(limit=6)
    statuses: list[tuple[bool, str]] = []
    client = make_client(connector, sleep=sleep, on_status=lambda c, d: statuses.append((c, d)))
    client.run()
    # 1, 2, 4 while failing; reset by the connection; then 1 after it drops,
    # 2 for the next failure, then 4 (the scripted connector is exhausted).
    assert sleep.delays == [1, 2, 4, 1, 2, 4]
    assert connection.sent == [{"type": "pong"}]
    assert connection.closed
    assert (True, "connected") in statuses
    assert statuses[-1] == (False, "stopped")
    assert connector.urls[0] == "ws://backend.test/ws?token=tok-1"
    assert not client.connected


def test_accept_then_close_does_not_reset_backoff() -> None:
    connector = FakeConnector([FakeConnection([]), FakeConnection([]), FakeConnection([])])
    sleep = FakeSleep(limit=3)
    client = make_client(connector, sleep=sleep)
    client.run()
    assert sleep.delays == [1, 2, 4]


def test_reconnect_delay_is_capped() -> None:
    sleep = FakeSleep(limit=8)
    client = make_client(FakeConnector([]), sleep=sleep)
    client.run()
    assert sleep.delays == [1, 2, 4, 8, 8, 8, 8, 8]


def test_rejected_token_is_refreshed_once_on_connect() -> None:
    log: list[httpx.Request] = []
    connection = FakeConnection([])
    connector = FakeConnector([AuthError("HTTP 403"), connection])
    sleep = FakeSleep(limit=1)
    client = make_client(connector, transport=rest_transport(log), sleep=sleep)
    client.run()
    assert connector.urls == [
        "ws://backend.test/ws?token=tok-1",
        "ws://backend.test/ws?token=tok-2",
    ]
    assert sleep.delays == [1]  # the refresh retried immediately, no backoff


def test_socket_closed_for_auth_refreshes_token() -> None:
    log: list[httpx.Request] = []
    revoked = FakeConnection([])
    revoked.inbound.put(AuthError("backend closed the socket (code 1008)"))
    connector = FakeConnector([revoked, FakeConnection([])])
    sleep = FakeSleep(limit=2)
    client = make_client(connector, transport=rest_transport(log), sleep=sleep)
    client.run()
    assert connector.urls[:2] == [
        "ws://backend.test/ws?token=tok-1",
        "ws://backend.test/ws?token=tok-2",
    ]


def test_auth_failure_without_secret_backs_off() -> None:
    sleep = FakeSleep(limit=2)
    connector = FakeConnector([AuthError("no"), AuthError("no")])
    client = make_client(connector, sleep=sleep, secret="", token="fixed")
    client.run()
    assert sleep.delays == [1, 2]
    assert connector.urls == ["ws://backend.test/ws?token=fixed"] * 2


def test_stop_during_connection_ends_loop() -> None:
    connection = FakeConnection(close_when_empty=False)
    client = make_client(FakeConnector([connection]))
    thread = client.start()
    deadline = time.monotonic() + 2
    while not client.connected and time.monotonic() < deadline:
        time.sleep(0.005)
    assert client.connected
    client.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert client.stopped
    assert connection.closed


# -- incoming frames --------------------------------------------------------


def test_events_are_dispatched() -> None:
    events: list[PushEvent] = []
    client = make_client(on_event=events.append)
    client.handle_message(
        json.dumps({"type": "event", "kind": "brief", "payload": {"text": "Morning brief."}})
    )
    client.handle_message(json.dumps({"type": "event", "kind": "alert", "payload": {"x": 1}}))
    client.handle_message(json.dumps({"type": "event", "payload": "bad"}))
    client.handle_message(json.dumps({"type": "event", "payload": {"text": "Note"}}))
    assert events == [
        PushEvent("brief", "Morning brief.", {"text": "Morning brief."}),
        PushEvent("notice", "Note", {"text": "Note"}),
    ]


def test_failing_event_handler_does_not_break_reader() -> None:
    def explode(_event: PushEvent) -> None:
        raise RuntimeError("boom")

    client = make_client(on_event=explode)
    client.handle_message(json.dumps({"type": "event", "kind": "notice", "payload": {"text": "x"}}))


def test_junk_frames_are_ignored() -> None:
    client = make_client()
    frames = ["not json", "[1, 2]", '{"type": "mystery"}', '{"type": "error"}', '{"type": "pong"}']
    for frame in frames:
        client.handle_message(frame)
    client.handle_message('{"type": "reply", "reply": "late", "conversation_id": "c9"}')
    assert client.conversation_id == "c9"


def test_pong_dropped_while_disconnected() -> None:
    client = make_client()
    assert client.send_json({"type": "pong"}) is False


# -- converse over the socket -----------------------------------------------


@pytest.fixture
def live_socket() -> Iterator[tuple[BackendClient, FakeConnection]]:
    turns = iter(["conv-1", "conv-1"])

    def responder(payload: dict[str, Any]) -> list[str]:
        if payload["type"] != "converse":
            return []
        if payload["text"] == "explode":
            return [json.dumps({"type": "error", "message": "agent crashed"})]
        if payload["text"] == "silence":
            return []
        return [
            json.dumps({"type": "event", "kind": "notice", "payload": {"text": "interleaved"}}),
            json.dumps(
                {
                    "type": "reply",
                    "conversation_id": next(turns),
                    "reply": f"WS heard: {payload['text']}",
                    "pending_actions": [],
                    "needs_confirmation": False,
                }
            ),
        ]

    connection = FakeConnection(responder=responder, close_when_empty=False)
    client = make_client(FakeConnector([connection]), reply_timeout=0.3)
    thread = client.start()
    deadline = time.monotonic() + 2
    while not client.connected and time.monotonic() < deadline:
        time.sleep(0.005)
    yield client, connection
    client.stop()
    thread.join(timeout=2)


def test_converse_over_websocket_keeps_conversation(
    live_socket: tuple[BackendClient, FakeConnection],
) -> None:
    client, connection = live_socket
    first = client.converse("what's next")
    assert first.text == "WS heard: what's next"
    assert client.conversation_id == "conv-1"
    client.converse("and then")
    assert connection.sent == [
        {"type": "converse", "text": "what's next", "conversation_id": None},
        {"type": "converse", "text": "and then", "conversation_id": "conv-1"},
    ]


def test_converse_error_frame(live_socket: tuple[BackendClient, FakeConnection]) -> None:
    client, _ = live_socket
    with pytest.raises(BackendError, match="agent crashed"):
        client.converse("explode")


def test_converse_timeout(live_socket: tuple[BackendClient, FakeConnection]) -> None:
    client, _ = live_socket
    with pytest.raises(BackendError, match="too long"):
        client.converse("silence")


def test_converse_falls_back_to_rest_when_send_fails(
    live_socket: tuple[BackendClient, FakeConnection],
) -> None:
    client, connection = live_socket
    connection.fail_send = True
    assert client.converse("hello").text == "REST heard: hello"


def test_connection_lost_while_waiting_fails_turn() -> None:
    def responder(payload: dict[str, Any]) -> list[str]:
        connection.inbound.put(ConnectionClosedError("dropped"))
        return []

    connection = FakeConnection(responder=responder, close_when_empty=False)
    sleep = FakeSleep(limit=1)
    client = make_client(FakeConnector([connection]), sleep=sleep, reply_timeout=2)
    thread = threading.Thread(target=client.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not client.connected and time.monotonic() < deadline:
        time.sleep(0.005)
    with pytest.raises(BackendError, match="connection lost"):
        client.converse("hi")
    thread.join(timeout=2)


# -- REST fallback ----------------------------------------------------------


def test_rest_fallback_when_disconnected() -> None:
    log: list[httpx.Request] = []
    client = make_client(transport=rest_transport(log))
    reply = client.converse("decline the 4 pm")
    assert reply.text == "REST heard: decline the 4 pm"
    assert reply.needs_confirmation
    assert reply.pending_actions == [{"id": "a1"}]
    assert client.conversation_id == "conv-rest"
    client.converse("yes")
    converse_calls = [r for r in log if r.url.path == "/converse"]
    assert [json.loads(r.content) for r in converse_calls] == [
        {"text": "decline the 4 pm", "conversation_id": None},
        {"text": "yes", "conversation_id": "conv-rest"},
    ]
    assert converse_calls[0].headers["authorization"] == "Bearer tok-1"


def test_rest_401_refreshes_token_once() -> None:
    log: list[httpx.Request] = []
    client = make_client(transport=rest_transport(log))
    client._tokens.get()  # tok-1
    client._tokens.invalidate()
    client._tokens.get()  # now tok-2; whoami only accepts tok-1
    with pytest.raises(AuthError):
        client.whoami()
    whoami_calls = [r.headers["authorization"] for r in log if r.url.path == "/auth/whoami"]
    assert whoami_calls == ["Bearer tok-2", "Bearer tok-3"]


def test_rest_401_with_fixed_token_raises() -> None:
    client = make_client(secret="", token="wrong")
    with pytest.raises(AuthError, match="rejected"):
        client.whoami()


def test_whoami_and_health() -> None:
    client = make_client()
    assert client.whoami() == "desktop"
    assert client.health() == {"status": "ok", "version": "0.1.0"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"converse_status": 500}, "HTTP 500"),
        ({"body": b"not json"}, "invalid JSON"),
        ({"body": b"[1]"}, "unexpected body"),
    ],
)
def test_rest_errors(kwargs: dict[str, Any], message: str) -> None:
    client = make_client(transport=rest_transport([], **kwargs))
    with pytest.raises(BackendError, match=message):
        client.converse("x")


def test_rest_network_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = make_client(transport=httpx.MockTransport(fail), token="t", secret="")
    with pytest.raises(BackendError, match="cannot reach"):
        client.converse("x")
    with pytest.raises(BackendError, match="cannot reach"):
        client.health()


def test_health_error_status() -> None:
    client = make_client(transport=httpx.MockTransport(lambda _r: httpx.Response(503)))
    with pytest.raises(BackendError, match="503"):
        client.health()


# -- helpers ----------------------------------------------------------------


def test_ws_url() -> None:
    assert ws_url("http://h:8000/", "a b/c") == "ws://h:8000/ws?token=a%20b%2Fc"
    assert ws_url("https://h", "t") == "wss://h/ws?token=t"
    assert ws_url("h", "t") == "h/ws?token=t"


def test_reply_from_json() -> None:
    reply = Reply.from_json({"text": "fallback field", "conversation_id": 7})
    assert reply.text == "fallback field"
    assert reply.conversation_id == "7"
    assert Reply.from_json({}).conversation_id is None


# -- the real websockets adapter against a local server ---------------------


@pytest.fixture
def ws_server() -> Iterator[str]:
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    from websockets.sync.server import ServerConnection, serve

    def process_request(connection: ServerConnection, request: Any) -> Response | None:
        if "token=bad" in request.path:
            return Response(401, "Unauthorized", Headers(), b"")
        if "token=teapot" in request.path:
            return Response(418, "Teapot", Headers(), b"")
        return None

    def handler(connection: ServerConnection) -> None:
        if "token=revoked" in (connection.request.path if connection.request else ""):
            connection.close(code=4401, reason="revoked")
            return
        connection.send(json.dumps({"type": "ping"}))
        for message in connection:
            connection.send(f"echo:{message}")
            connection.send(b"binary-frame")

    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.socket.getsockname()[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"ws://127.0.0.1:{port}/ws"
        server.shutdown()


def test_websockets_connector_roundtrip(ws_server: str) -> None:
    conn = websockets_connector(ws_server + "?token=good")
    assert json.loads(conn.recv(timeout=2)) == {"type": "ping"}
    conn.send("hello")
    assert conn.recv(timeout=2) == "echo:hello"
    assert conn.recv(timeout=2) == "binary-frame"
    with pytest.raises(TimeoutError):
        conn.recv(timeout=0.05)
    conn.close()
    with pytest.raises(ConnectionClosedError):
        conn.recv(timeout=1)
    with pytest.raises(ConnectionClosedError):
        conn.send("after close")


def test_websockets_connector_auth_errors(ws_server: str) -> None:
    with pytest.raises(AuthError, match="401"):
        websockets_connector(ws_server + "?token=bad")
    with pytest.raises(ConnectionClosedError, match="418"):
        websockets_connector(ws_server + "?token=teapot")
    conn = websockets_connector(ws_server + "?token=revoked")
    with pytest.raises(AuthError, match="4401"):
        conn.recv(timeout=2)
