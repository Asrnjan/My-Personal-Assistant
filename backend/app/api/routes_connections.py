"""Connecting Claude, task tools and chat apps from the app, so nobody edits ``.env``."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.auth import AuthenticatedClient
from app.integrations.connections import (
    APPS,
    BY_ID,
    AppSpec,
    ConnectionService,
    SetupError,
)

router = APIRouter(prefix="/connections", tags=["connections"])


class FieldOut(BaseModel):
    key: str
    label: str
    secret: bool
    optional: bool
    placeholder: str
    help: str
    value: str  # empty for secrets
    filled: bool


class AppOut(BaseModel):
    id: str
    name: str
    group: str
    description: str
    steps: list[str]
    link: str
    link_label: str
    connected: bool
    source: str | None
    fields: list[FieldOut]


class ConnectionsOut(BaseModel):
    ready: bool  # FERNET_KEY is set
    apps: list[AppOut]


class ConnectIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict, max_length=16)


class ConnectOut(BaseModel):
    message: str
    app: AppOut


def _service(request: Request) -> ConnectionService:
    service: ConnectionService | None = request.app.state.connections
    if service is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "FERNET_KEY is not set, so apps can't be connected yet",
        )
    return service


def _app_out(service: ConnectionService, spec: AppSpec) -> AppOut:
    current = service.current(spec)
    return AppOut(
        id=spec.id,
        name=spec.name,
        group=spec.group,
        description=spec.description,
        steps=list(spec.steps),
        link=spec.link,
        link_label=spec.link_label,
        connected=service.is_connected(spec),
        source=service.source(spec),
        fields=[
            FieldOut(
                key=f.key,
                label=f.label,
                secret=f.secret,
                optional=f.optional,
                placeholder=f.placeholder,
                help=f.help,
                value="" if f.secret else current.get(f.key, ""),
                filled=bool(current.get(f.key)),
            )
            for f in spec.fields
        ],
    )


@router.get("", response_model=ConnectionsOut)
def list_connections(request: Request, _client: AuthenticatedClient) -> ConnectionsOut:
    service: ConnectionService | None = request.app.state.connections
    if service is None:
        return ConnectionsOut(ready=False, apps=[])
    return ConnectionsOut(ready=True, apps=[_app_out(service, a) for a in APPS])


@router.put("/{app_id}", response_model=ConnectOut)
def connect(
    app_id: str, body: ConnectIn, request: Request, _client: AuthenticatedClient
) -> ConnectOut:
    """Check the details with the app itself, save them encrypted and start using them."""
    spec = BY_ID.get(app_id)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown app")
    service = _service(request)
    try:
        checked = service.connect(app_id, body.values)
    except SetupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return ConnectOut(message=checked.message, app=_app_out(service, spec))


@router.delete("/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(app_id: str, request: Request, _client: AuthenticatedClient) -> Response:
    if app_id not in BY_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown app")
    _service(request).disconnect(app_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
