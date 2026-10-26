"""Files the assistant made (presentations), for the signed-in app to download."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from app.api.auth import AuthenticatedClient

router = APIRouter(tags=["files"])


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
