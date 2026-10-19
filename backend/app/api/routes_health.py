"""Liveness and readiness."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
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
    # Shown in the apps and listened for ("Atlas, brief me").
    assistant_name: str = "Atlas"


class WhoAmI(BaseModel):
    client_name: str


@router.get("/health", response_model=Health)
def health(db: DbSession, request: Request) -> Health:
    name = request.app.state.app_config.assistant_name
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return Health(status="degraded", version=__version__, database="error", assistant_name=name)
    return Health(status="ok", version=__version__, database="ok", assistant_name=name)


@router.get("/auth/whoami", response_model=WhoAmI, tags=["auth"])
def whoami(client: AuthenticatedClient) -> WhoAmI:
    return WhoAmI(client_name=client.client_name)
