"""Unified record for tasks, meetings and actionable emails."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import ItemKind, ItemStatus, PriorityBand, Source


class ItemBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: Source
    source_id: str = Field(min_length=1)
    kind: ItemKind
    title: str = Field(min_length=1)
    description: str = ""
    url: str | None = None
    requester: str | None = None
    participants: list[str] = Field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None
    due: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=0)
    status: ItemStatus = ItemStatus.OPEN

    @model_validator(mode="after")
    def _check_times(self) -> ItemBase:
        if self.start and self.end and self.end < self.start:
            raise ValueError("end must not be before start")
        return self


class ItemScore(BaseModel):
    """The output of the priority engine for one item."""

    priority_score: int = Field(ge=0, le=100)
    priority_band: PriorityBand
    reason: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    ceo_required: bool
    suggested_delegate_id: str | None = None


class Item(ItemBase):
    id: str
    priority_score: int | None = Field(default=None, ge=0, le=100)
    priority_band: PriorityBand | None = None
    reason: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    ceo_required: bool | None = None
    suggested_delegate_id: str | None = None
    created_at: datetime
    updated_at: datetime
    last_synced_at: datetime | None = None
