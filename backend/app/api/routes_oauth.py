"""Connecting Google and Microsoft accounts."""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.api.auth import AuthenticatedClient
from app.integrations.http import IntegrationError
from app.integrations.oauth import OAuthManager

router = APIRouter(prefix="/oauth", tags=["oauth"])


class StartResponse(BaseModel):
    authorization_url: str


def _manager(request: Request) -> OAuthManager:
    manager: OAuthManager | None = request.app.state.oauth
    if manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "FERNET_KEY is not set")
    return manager


@router.get("/{provider}/start", response_model=StartResponse)
def start(provider: str, request: Request, _client: AuthenticatedClient) -> StartResponse:
    """Returns the provider's consent URL; the client opens it in a browser."""
    try:
        url = _manager(request).authorization_url(provider)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return StartResponse(authorization_url=url)


def _page(title: str, message: str, code: int, back: str = "/") -> HTMLResponse:
    # The app is served from "/", so the button (and, on success, a short redirect)
    # takes you straight back to it.
    refresh = f"<meta http-equiv=refresh content='2;url={escape(back)}'>" if code == 200 else ""
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"{refresh}<title>{escape(title)}</title>"
        "<body style='font-family:system-ui;padding:2rem;background:#0b1020;color:#e6ecff'>"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p>"
        f"<p><a href='{escape(back)}' style='color:#7cd4ff'>Back to your assistant</a></p>"
    )
    return HTMLResponse(body, status_code=code)


@router.get("/{provider}/callback", response_class=HTMLResponse)
def callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Redirect target after consent. Protected by the one-time ``state`` value."""
    if error:
        return _page("Not connected", f"{provider} returned: {error}", 400)
    if not code or not state:
        return _page("Not connected", "The response was missing a code or state.", 400)
    try:
        _manager(request).handle_callback(provider, code, state)
    except IntegrationError as exc:
        return _page("Not connected", str(exc), 400)
    return _page(
        "Connected",
        f"Your {provider} account is connected. Taking you back to your assistant…",
        200,
        back=f"/?connected={provider}",
    )
