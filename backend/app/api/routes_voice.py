"""Server-side speech for clients without a speech engine of their own."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.api.auth import AuthenticatedClient
from app.engine.conversation import spoken_reply
from app.voice import SpeechToText, TextToSpeech, VoiceUnavailableError

router = APIRouter(tags=["voice"])

# 10 MB is several minutes of compressed speech.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TTS_CHARS = 4000


class Transcript(BaseModel):
    text: str


@router.post("/stt", response_model=Transcript)
async def speech_to_text(
    request: Request, _client: AuthenticatedClient, audio: Annotated[UploadFile, File()]
) -> Transcript:
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "audio is too long")
    stt: SpeechToText = request.app.state.stt
    try:
        text = await run_in_threadpool(stt.transcribe, data)
    except VoiceUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return Transcript(text=text)


@router.get("/tts", response_class=Response, responses={200: {"content": {"audio/wav": {}}}})
async def text_to_speech(
    request: Request,
    _client: AuthenticatedClient,
    text: Annotated[str, Query(min_length=1, max_length=MAX_TTS_CHARS)],
) -> Response:
    tts: TextToSpeech = request.app.state.tts
    try:
        wav = await run_in_threadpool(tts.synthesize, spoken_reply(text))
    except VoiceUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})
