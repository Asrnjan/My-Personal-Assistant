"""Connecting accounts from the app: Google, Outlook and Teams.

The app lists what can be connected, saves the one-time OAuth app keys (so nobody edits
``.env``), and starts the sign-in that ends at ``/oauth/<provider>/callback``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.auth import AuthenticatedClient
from app.integrations.http import IntegrationError
from app.integrations.oauth import TEAMS_SCOPES, AppKeys, OAuthManager

router = APIRouter(prefix="/accounts", tags=["accounts"])


@dataclass(frozen=True)
class AccountKind:
    id: str
    name: str
    description: str
    provider: str
    extra_scopes: tuple[str, ...] = ()
    # Lower-case scope that shows this account is connected (beyond the provider's own).
    marker_scope: str | None = None


ACCOUNTS = (
    AccountKind("google", "Google", "Gmail and Google Calendar", "google"),
    AccountKind("outlook", "Outlook", "Outlook email and calendar", "microsoft"),
    AccountKind(
        "teams",
        "Microsoft Teams",
        "Read and send your Teams chats (work or school account)",
        "microsoft",
        extra_scopes=TEAMS_SCOPES,
        marker_scope="chat.readwrite",
    ),
)
_BY_ID = {a.id: a for a in ACCOUNTS}
PROVIDER_NAMES = {"google": "Google", "microsoft": "Microsoft"}


class AccountOut(BaseModel):
    id: str
    name: str
    description: str
    provider: str
    connected: bool
    ready: bool  # the provider's app keys are set, so Connect can start


class ProviderOut(BaseModel):
    id: str
    name: str
    configured: bool
    keys_source: str | None
    redirect_uri: str
    tenant_id: str | None = None


class AccountsOut(BaseModel):
    oauth_ready: bool  # FERNET_KEY is set
    accounts: list[AccountOut]
    providers: list[ProviderOut]


class AppKeysIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: str = Field(min_length=1, max_length=512)
    tenant_id: str | None = Field(default=None, max_length=64)


class ConnectOut(BaseModel):
    authorization_url: str


def _oauth(request: Request) -> OAuthManager | None:
    manager: OAuthManager | None = request.app.state.oauth
    return manager


def _require_oauth(request: Request) -> OAuthManager:
    manager = _oauth(request)
    if manager is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "FERNET_KEY is not set, so accounts can't be connected yet",
        )
    return manager


def _connected(manager: OAuthManager, kind: AccountKind) -> bool:
    granted = manager.granted_scopes(kind.provider)
    if not manager.is_connected(kind.provider):
        return False
    return kind.marker_scope is None or kind.marker_scope in granted


@router.get("", response_model=AccountsOut)
def list_accounts(request: Request, _client: AuthenticatedClient) -> AccountsOut:
    manager = _oauth(request)
    if manager is None:
        return AccountsOut(
            oauth_ready=False,
            accounts=[
                AccountOut(
                    id=a.id,
                    name=a.name,
                    description=a.description,
                    provider=a.provider,
                    connected=False,
                    ready=False,
                )
                for a in ACCOUNTS
            ],
            providers=[],
        )
    keys = manager.app_keys
    providers = []
    for pid, name in PROVIDER_NAMES.items():
        saved = keys.load(pid) if keys is not None else None
        providers.append(
            ProviderOut(
                id=pid,
                name=name,
                configured=manager.is_configured(pid),
                keys_source=manager.keys_source(pid),
                redirect_uri=manager.redirect_uri(pid),
                tenant_id=saved.tenant_id if saved else None,
            )
        )
    return AccountsOut(
        oauth_ready=True,
        accounts=[
            AccountOut(
                id=a.id,
                name=a.name,
                description=a.description,
                provider=a.provider,
                connected=_connected(manager, a),
                ready=manager.is_configured(a.provider),
            )
            for a in ACCOUNTS
        ],
        providers=providers,
    )


@router.put("/apps/{provider}", status_code=status.HTTP_204_NO_CONTENT)
def save_app_keys(
    provider: str, body: AppKeysIn, request: Request, _client: AuthenticatedClient
) -> Response:
    """Save the one-time OAuth app keys from the provider's developer console."""
    if provider not in PROVIDER_NAMES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")
    manager = _require_oauth(request)
    if manager.app_keys is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app keys can't be stored")
    tenant = (body.tenant_id or "").strip() or None
    manager.app_keys.save(
        provider, AppKeys(body.client_id.strip(), body.client_secret.strip(), tenant)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{account}/connect", response_model=ConnectOut)
def connect(account: str, request: Request, _client: AuthenticatedClient) -> ConnectOut:
    """The sign-in page to open. Teams asks for its scopes on top of Outlook's."""
    kind = _BY_ID.get(account)
    if kind is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown account")
    manager = _require_oauth(request)
    if not manager.is_configured(kind.provider):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Add the {PROVIDER_NAMES[kind.provider]} app keys first (one-time setup).",
        )
    extra = kind.extra_scopes
    # Reconnecting Outlook must not drop Teams access that was already granted.
    if kind.provider == "microsoft" and "chat.readwrite" in manager.granted_scopes("microsoft"):
        extra = TEAMS_SCOPES
    try:
        url = manager.authorization_url(kind.provider, extra)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return ConnectOut(authorization_url=url)


@router.delete("/{account}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(account: str, request: Request, _client: AuthenticatedClient) -> Response:
    """Forget the tokens. Outlook and Teams share one Microsoft sign-in, so both go."""
    kind = _BY_ID.get(account)
    if kind is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown account")
    _require_oauth(request).disconnect(kind.provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
