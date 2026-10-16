"""Liveness and readiness."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.auth import AuthenticatedClient
from app.api.deps import DbSession

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: Literal["ok", "error"]


class WhoAmI(BaseModel):
    client_name: str


@router.get("/health", response_model=Health)
def health(db: DbSession) -> Health:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return Health(status="degraded", version=__version__, database="error")
    return Health(status="ok", version=__version__, database="ok")


@router.get("/auth/whoami", response_model=WhoAmI, tags=["auth"])
def whoami(client: AuthenticatedClient) -> WhoAmI:
    return WhoAmI(client_name=client.client_name)
