"""Talking to the assistant, and approving what it proposes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import AuthenticatedClient
from app.domain.enums import ActionStatus, ApprovedVia
from app.engine.actions import ActionError, PendingAction
from app.engine.conversation import ConverseResponse

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
