"""``WS /ws``: pushed briefs, alerts and notices, plus conversation turns.

Authenticate with ``?token=<bearer token>`` (browsers can't set headers on a
WebSocket) or an ``Authorization: Bearer`` header.

Server → client: ``{"type": "event", "kind": "morning"|"evening"|"alert"|"notice",
"payload": {...}}``, ``{"type": "reply", ...ConverseResponse}``, ``{"type": "pong"}``,
``{"type": "error", "message": ...}``.
Client → server: ``{"type": "converse", "text": ..., "conversation_id": ...}``,
``{"type": "ping"}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.api.auth import hash_token
from app.db.models import ApiTokenRow
from app.domain.enums import ApprovedVia
from app.events import Event

log = logging.getLogger(__name__)
router = APIRouter()

QUEUE_SIZE = 100


def event_message(event: Event) -> dict[str, Any]:
    kind = str(event.payload.get("kind", event.kind)) if event.kind == "brief" else event.kind
    return {"type": "event", "kind": kind, "payload": event.payload}


def _client_for(websocket: WebSocket) -> ApiTokenRow | None:
    token = websocket.query_params.get("token", "")
    header = websocket.headers.get("authorization", "")
    if not token and header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        return None
    with websocket.app.state.session_factory() as s:
        row: ApiTokenRow | None = s.scalar(
            select(ApiTokenRow).where(
                ApiTokenRow.token_hash == hash_token(token), ApiTokenRow.revoked_at.is_(None)
            )
        )
        return row


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    client = await run_in_threadpool(_client_for, websocket)
    if client is None:
        # Accept first so every client sees the close code (1008 = bad or missing token).
        await websocket.accept()
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)

    def offer(message: dict[str, Any]) -> None:
        if queue.full():
            log.warning("dropping a push event for a slow WebSocket client")
            return
        queue.put_nowait(message)

    def on_event(event: Event) -> None:
        # Called from whichever thread published; hand over to this connection's loop.
        loop.call_soon_threadsafe(offer, event_message(event))

    unsubscribe = websocket.app.state.events.subscribe(on_event)

    async def sender() -> None:
        while True:
            await websocket.send_json(await queue.get())

    send_task = asyncio.create_task(sender())
    agent = websocket.app.state.agent
    default_conversation = f"client-{client.id}"
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "ping":
                offer({"type": "pong"})
            elif kind == "converse":
                text = str(message.get("text", ""))[:2000]
                conversation = str(message.get("conversation_id") or default_conversation)[:64]
                response = await run_in_threadpool(
                    agent.converse, text, conversation, ApprovedVia.VOICE
                )
                offer({"type": "reply", **response.model_dump(mode="json")})
            else:
                offer({"type": "error", "message": "unknown message type"})
    except WebSocketDisconnect:
        pass
    except ValueError:
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
    finally:
        unsubscribe()
        send_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await send_task
