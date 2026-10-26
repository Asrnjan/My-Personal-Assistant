"""Talking to the assistant, and approving what it proposes."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import AuthenticatedClient
from app.domain.enums import ActionStatus, ApprovedVia
from app.engine.actions import ActionError, PendingAction
from app.engine.conversation import ConverseResponse

log = logging.getLogger(__name__)
router = APIRouter(tags=["conversation"])


class ConverseRequest(BaseModel):
    text: str = Field(max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)


@router.post("/converse", response_model=ConverseResponse)
def converse(
    body: ConverseRequest, request: Request, client: AuthenticatedClient
) -> ConverseResponse:
    # One ongoing conversation per client unless the client names its own.
    conversation_id = body.conversation_id or f"client-{client.id}"
    response: ConverseResponse = request.app.state.agent.converse(
        body.text, conversation_id, ApprovedVia.VOICE
    )
    return response


@router.post("/converse/stream")
async def converse_stream(
    body: ConverseRequest, request: Request, client: AuthenticatedClient
) -> StreamingResponse:
    """Like ``/converse``, but as newline-delimited JSON: a ``{"type": "say"}`` line for each
    sentence as soon as it's written, so the app can start speaking, then one
    ``{"type": "done", ...}`` line with the whole response (its ``rest`` is what's left to
    say)."""
    conversation_id = body.conversation_id or f"client-{client.id}"
    agent = request.app.state.agent
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    def say(text: str) -> None:
        line = json.dumps({"type": "say", "text": text}) + "\n"
        loop.call_soon_threadsafe(lines.put_nowait, line)

    async def answer() -> None:
        try:
            response: ConverseResponse = await run_in_threadpool(
                agent.converse, body.text, conversation_id, ApprovedVia.VOICE, on_say=say
            )
            line = json.dumps({"type": "done", **response.model_dump(mode="json")})
        except Exception:
            log.exception("streamed conversation failed")
            line = json.dumps({"type": "error", "detail": "Something went wrong. Try again."})
        await lines.put(line + "\n")
        await lines.put(None)

    task = asyncio.create_task(answer())

    async def stream() -> AsyncIterator[str]:
        while (line := await lines.get()) is not None:
            yield line
        await task

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class ConfirmResponse(BaseModel):
    action: PendingAction
    ok: bool
    message: str


@router.get("/actions", response_model=list[PendingAction])
def list_actions(
    request: Request,
    _client: AuthenticatedClient,
    status_: Annotated[ActionStatus | None, Query(alias="status")] = None,
) -> list[PendingAction]:
    found: list[PendingAction] = request.app.state.actions.find(status_)
    return found


@router.post("/actions/{action_id}/confirm", response_model=ConfirmResponse)
def confirm(action_id: str, request: Request, _client: AuthenticatedClient) -> ConfirmResponse:
    actions = request.app.state.actions
    try:
        result = actions.confirm(action_id, ApprovedVia.TAP)
    except ActionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return ConfirmResponse(action=actions.get(action_id), ok=result.ok, message=result.message)


@router.post("/actions/{action_id}/cancel", response_model=PendingAction)
def cancel(action_id: str, request: Request, _client: AuthenticatedClient) -> PendingAction:
    try:
        action: PendingAction = request.app.state.actions.cancel(action_id)
    except ActionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return action
