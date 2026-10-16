"""Morning brief, evening wrap-up and delivered-brief history."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select

from app.api.auth import AuthenticatedClient
from app.api.deps import DbSession
from app.db.models import BriefRow
from app.engine.briefing import Brief, BriefKind

router = APIRouter(prefix="/brief", tags=["brief"])


@router.get("/today", response_model=Brief)
def today(
    request: Request,
    _client: AuthenticatedClient,
    kind: BriefKind = BriefKind.MORNING,
    on: Annotated[date | None, Query(alias="date")] = None,
) -> Brief:
    """Build a brief on demand from current data. Nothing is stored or pushed."""
    briefer = request.app.state.briefer
    if kind == BriefKind.MORNING:
        brief: Brief = briefer.morning(on)
    elif kind == BriefKind.EVENING:
        brief = briefer.evening(on)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "use /brief/latest?kind=alert for alerts")
    return brief


@router.get("/latest", response_model=Brief)
def latest(db: DbSession, _client: AuthenticatedClient, kind: BriefKind | None = None) -> Brief:
    """The most recently delivered brief, for clients that missed the push."""
    stmt = select(BriefRow).order_by(BriefRow.created_at.desc()).limit(1)
    if kind is not None:
        stmt = stmt.where(BriefRow.kind == kind.value)
    row = db.scalars(stmt).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no brief delivered yet")
    return Brief.model_validate(row.data)


@router.post("/deliver", response_model=Brief)
def deliver(request: Request, _client: AuthenticatedClient, kind: BriefKind) -> Brief:
    """Run the scheduled brief job now: build, store and push it.

    Lets cron or GitHub Actions drive briefs when the built-in scheduler is off.
    """
    if kind == BriefKind.ALERT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "alerts are sent by POST /sync")
    brief: Brief = request.app.state.jobs.deliver_kind(kind)
    return brief
