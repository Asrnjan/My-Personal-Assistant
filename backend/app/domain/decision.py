"""Decisions taken on items and the follow-ups they create."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Actor, ApprovedVia, Channel, DecisionAction, FollowUpState


class Decision(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    item_id: str
    action: DecisionAction
    actor: Actor
    delegate_id: str | None = None
    brief_text: str | None = None
    channels_used: list[Channel] = Field(default_factory=list)
    approved_via: ApprovedVia
    executed_at: datetime | None = None
    result: str | None = None
    error: str | None = None


class FollowUp(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    decision_id: str
    due: datetime | None = None
    acknowledged_at: datetime | None = None
    last_nudge_at: datetime | None = None
    nudges_sent: int = Field(default=0, ge=0)
    state: FollowUpState = FollowUpState.PENDING
