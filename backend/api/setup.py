"""Setup from the app's Settings window, so nobody needs a terminal: the
Gemini key and the Google OAuth client file.
Secrets go to the Keychain; nothing here ever returns one."""

from __future__ import annotations

from typing import Any

import httpx
import keyring
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.auth import require_token
from backend.integrations.google_auth import KEYCHAIN_SERVICE, GoogleAuth
from backend.voice.gemini import API as GEMINI_API
from backend.voice.gemini import GEMINI_KEY, api_key

router = APIRouter(prefix="/setup", dependencies=[Depends(require_token)])


class GeminiKey(BaseModel):
    key: str


class GoogleClient(BaseModel):
    json_text: str


@router.get("")
async def status(request: Request) -> dict[str, Any]:
    state = request.app.state
    google: GoogleAuth = state.google
    return {
        "gemini_key": api_key() is not None,
        "brain_ready": state.llm.available,
        "google": {"configured": google.configured, "connected": google.connected},
    }


@router.put("/gemini-key")
async def set_gemini_key(body: GeminiKey, request: Request) -> dict[str, Any]:
    key = body.key.strip()
    # Check the key with Google before saving it, so a typo fails here, not mid-conversation.
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(GEMINI_API, params={"pageSize": 1}, headers={"x-goog-api-key": key})
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Couldn't reach Google to check the key: {e}") from e
    if r.status_code != 200:
        raise HTTPException(400, "Google didn't accept that key. Copy it again from aistudio.google.com.")
    keyring.set_password(KEYCHAIN_SERVICE, GEMINI_KEY, key)
    request.app.state.llm.select("gemini")  # adding a key means they want Gemini
    return await status(request)


@router.delete("/gemini-key")
async def delete_gemini_key(request: Request) -> dict[str, Any]:
    if api_key():
        keyring.delete_password(KEYCHAIN_SERVICE, GEMINI_KEY)
    return await status(request)


@router.post("/google-client")
async def set_google_client(body: GoogleClient, request: Request) -> dict[str, Any]:
    try:
        GoogleAuth.save_client_config(body.json_text)
    except ValueError as e:
        raise HTTPException(400, f"That isn't the OAuth client file from Google Cloud: {e}") from e
    return await status(request)
