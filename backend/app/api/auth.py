"""Bearer-token authentication for clients.

A client exchanges the shared ``API_AUTH_TOKEN`` for its own token via
``POST /auth/token``. Only a SHA-256 hash of issued tokens is stored, so a
leaked database does not leak usable tokens, and each client can be revoked.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbSession, SettingsDep
from app.db.models import ApiTokenRow

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


class TokenRequest(BaseModel):
    secret: str = Field(min_length=1)
    client_name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    client_name: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/token", response_model=TokenResponse)
def issue_token(body: TokenRequest, db: DbSession, settings: SettingsDep) -> TokenResponse:
    expected = settings.api_auth_token.get_secret_value()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_AUTH_TOKEN is not set")
    if not hmac.compare_digest(body.secret.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid secret")
    token = secrets.token_urlsafe(32)
    db.add(ApiTokenRow(client_name=body.client_name, token_hash=hash_token(token)))
    return TokenResponse(access_token=token, client_name=body.client_name)


def require_client(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ApiTokenRow:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    row = db.scalar(
        select(ApiTokenRow).where(
            ApiTokenRow.token_hash == hash_token(credentials.credentials),
            ApiTokenRow.revoked_at.is_(None),
        )
    )
    if row is None:
        raise unauthorized
    return row


AuthenticatedClient = Annotated[ApiTokenRow, Depends(require_client)]
