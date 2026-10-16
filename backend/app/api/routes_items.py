"""Manual sync and item listing."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import and_, or_, select

from app.api.auth import AuthenticatedClient
from app.api.deps import DbSession
from app.db.models import ItemRow
from app.domain.enums import ItemKind, ItemStatus, PriorityBand
from app.domain.item import Item
from app.engine.ingest import Ingestor
from app.integrations.registry import SyncStateStore

router = APIRouter(tags=["items"])


class SyncReport(BaseModel):
    created: int
    updated: int
    closed: int
    duplicates: int
    errors: list[str]
    not_configured: list[str]
    scored: int
    escalated: int
    rules_only: bool


@router.post("/sync", response_model=SyncReport)
def sync(request: Request, _client: AuthenticatedClient) -> SyncReport:
    state = request.app.state
    ingestor = Ingestor(
        state.adapters_factory(),
        state.session_factory,
        SyncStateStore(state.session_factory, state.clock),
        state.app_config,
        state.clock,
    )
    report = ingestor.run()
    scoring = state.priority.score_pending()
    return SyncReport(
        scored=scoring.scored,
        escalated=scoring.escalated,
        rules_only=scoring.rules_only,
        created=report.created,
        updated=report.updated,
        closed=report.closed,
        duplicates=report.duplicates,
        errors=[*report.errors, *scoring.errors],
        not_configured=report.not_configured,
    )


@router.get("/items", response_model=list[Item])
def list_items(
    request: Request,
    db: DbSession,
    _client: AuthenticatedClient,
    on: Annotated[date | None, Query(alias="date")] = None,
    band: PriorityBand | None = None,
    status_: Annotated[ItemStatus | None, Query(alias="status")] = ItemStatus.OPEN,
) -> list[Item]:
    """Items, open by default. ``date`` (in the configured timezone) keeps
    meetings starting and tasks due that day, plus open emails."""
    stmt = select(ItemRow)
    if status_ is not None:
        stmt = stmt.where(ItemRow.status == status_)
    if band is not None:
        stmt = stmt.where(ItemRow.priority_band == band)
    if on is not None:
        tz = request.app.state.app_config.tz
        lo = datetime.combine(on, time.min, tzinfo=tz)
        hi = lo + timedelta(days=1)
        stmt = stmt.where(
            or_(
                and_(ItemRow.start >= lo, ItemRow.start < hi),
                and_(ItemRow.due >= lo, ItemRow.due < hi),
                ItemRow.kind == ItemKind.EMAIL_ACTION,
            )
        )
    stmt = stmt.order_by(
        ItemRow.priority_score.desc().nulls_last(), ItemRow.start, ItemRow.due, ItemRow.title
    )
    return [Item.model_validate(r) for r in db.scalars(stmt)]


@router.get("/items/{item_id}", response_model=Item)
def get_item(item_id: str, db: DbSession, _client: AuthenticatedClient) -> Item:
    row = db.get(ItemRow, item_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    return Item.model_validate(row)
