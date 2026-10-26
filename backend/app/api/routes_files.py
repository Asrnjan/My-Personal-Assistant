"""Files the assistant made (presentations), for the signed-in app to download."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.auth import AuthenticatedClient

router = APIRouter(tags=["files"])


class FileInfo(BaseModel):
    id: str
    name: str
    slides: int
    created_at: datetime | None


@router.get("/files", response_model=list[FileInfo])
def list_files(request: Request, _client: AuthenticatedClient) -> list[FileInfo]:
    """Files the assistant made, newest first, for the app's "Your files" list."""
    documents = request.app.state.agent.skills.documents
    if documents is None:
        return []
    return [
        FileInfo(id=f.id, name=f.name, slides=f.slides, created_at=f.created_at)
        for f in documents.recent(20)
    ]


@router.get("/files/{file_id}", response_class=FileResponse)
def download(file_id: str, request: Request, _client: AuthenticatedClient) -> FileResponse:
    documents = request.app.state.agent.skills.documents
    made = documents.get(file_id) if documents is not None else None
    if made is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file.")
    return FileResponse(
        made.path,
        media_type=made.media_type,
        filename=made.name,
        headers={"Cache-Control": "no-store"},
    )
