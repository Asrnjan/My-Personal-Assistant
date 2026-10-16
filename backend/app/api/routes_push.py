"""Web Push: the VAPID public key and subscription management for the PWA."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import AuthenticatedClient
from app.push import WebPusher

router = APIRouter(prefix="/push", tags=["push"])


class VapidKey(BaseModel):
    key: str


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=64)


class Subscription(BaseModel):
    # ``PushSubscription.toJSON()`` also sends ``expirationTime``; it isn't needed.
    model_config = ConfigDict(extra="ignore")

    endpoint: str = Field(pattern=r"^https://", max_length=1024)
    keys: SubscriptionKeys


class Unsubscribe(BaseModel):
    endpoint: str = Field(max_length=1024)


def _pusher(request: Request) -> WebPusher:
    pusher: WebPusher | None = request.app.state.pusher
    if pusher is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Web Push isn't set up: add VAPID keys to .env (docs/SETUP.md)",
        )
    return pusher


@router.get("/vapid-public-key", response_model=VapidKey)
def vapid_public_key(request: Request, _client: AuthenticatedClient) -> VapidKey:
    _pusher(request)
    return VapidKey(key=request.app.state.settings.vapid_public_key)


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
def subscribe(body: Subscription, request: Request, client: AuthenticatedClient) -> Response:
    _pusher(request).subscribe(body.endpoint, body.keys.p256dh, body.keys.auth, client.id)
    return Response(status_code=status.HTTP_201_CREATED)


@router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe(body: Unsubscribe, request: Request, _client: AuthenticatedClient) -> Response:
    if not _pusher(request).unsubscribe(body.endpoint):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such subscription")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
