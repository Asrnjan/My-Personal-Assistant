"""Delegates, follow-ups and learned preferences."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.api.auth import AuthenticatedClient
from app.api.deps import DbSession
from app.audit import audit
from app.db.models import DecisionRow, DelegateRow, ItemRow
from app.domain.delegate import Delegate, DelegateBase
from app.engine.followup import FollowUpView
from app.engine.learning import Preference

router = APIRouter()


@router.get("/delegates", response_model=list[Delegate], tags=["delegates"])
def list_delegates(db: DbSession, _client: AuthenticatedClient) -> list[Delegate]:
    rows = db.scalars(select(DelegateRow).order_by(DelegateRow.full_name))
    return [Delegate.model_validate(r) for r in rows]


@router.get("/delegates/{delegate_id}", response_model=Delegate, tags=["delegates"])
def get_delegate(delegate_id: str, db: DbSession, _client: AuthenticatedClient) -> Delegate:
    row = db.get(DelegateRow, delegate_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delegate not found")
    return Delegate.model_validate(row)


@router.post(
    "/delegates",
    response_model=Delegate,
    status_code=status.HTTP_201_CREATED,
    tags=["delegates"],
)
def create_delegate(body: Delegate, db: DbSession, _client: AuthenticatedClient) -> Delegate:
    if db.get(DelegateRow, body.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a delegate with this id exists")
    db.add(DelegateRow(**body.model_dump(mode="json")))
    audit(db, "delegate.create", "ok", target=body.id, actor="ceo")
    return body


@router.put("/delegates/{delegate_id}", response_model=Delegate, tags=["delegates"])
def update_delegate(
    delegate_id: str, body: DelegateBase, db: DbSession, _client: AuthenticatedClient
) -> Delegate:
    row = db.get(DelegateRow, delegate_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delegate not found")
    for key, value in body.model_dump(mode="json").items():
        setattr(row, key, value)
    audit(db, "delegate.update", "ok", target=delegate_id, actor="ceo")
    return Delegate.model_validate(row)


@router.delete(
    "/delegates/{delegate_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["delegates"]
)
def delete_delegate(delegate_id: str, db: DbSession, _client: AuthenticatedClient) -> Response:
    row = db.get(DelegateRow, delegate_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delegate not found")
    # Past decisions keep their brief and the audit log keeps the id.
    db.execute(
        update(ItemRow)
        .where(ItemRow.suggested_delegate_id == delegate_id)
        .values(suggested_delegate_id=None)
    )
    db.execute(
        update(DecisionRow).where(DecisionRow.delegate_id == delegate_id).values(delegate_id=None)
    )
    db.delete(row)
    audit(db, "delegate.delete", "ok", target=delegate_id, actor="ceo")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/followups", response_model=list[FollowUpView], tags=["delegates"])
def list_followups(
    request: Request,
    _client: AuthenticatedClient,
    open_only: Annotated[bool, Query(alias="open")] = False,
) -> list[FollowUpView]:
    views: list[FollowUpView] = request.app.state.followups.views(open_only)
    return views


class PreferenceRequest(BaseModel):
    rule: str = Field(min_length=1, max_length=500)


@router.get("/preferences", response_model=list[Preference], tags=["preferences"])
def list_preferences(request: Request, _client: AuthenticatedClient) -> list[Preference]:
    prefs: list[Preference] = request.app.state.learning.views()
    return prefs


@router.post(
    "/preferences",
    response_model=Preference,
    status_code=status.HTTP_201_CREATED,
    tags=["preferences"],
)
def add_preference(
    body: PreferenceRequest, request: Request, _client: AuthenticatedClient
) -> Preference:
    pref: Preference = request.app.state.learning.add_rule(body.rule)
    return pref


@router.delete(
    "/preferences/{preference_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["preferences"],
)
def delete_preference(
    preference_id: str, request: Request, _client: AuthenticatedClient
) -> Response:
    if not request.app.state.learning.delete(preference_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "preference not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
