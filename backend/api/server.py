from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape
from typing import Any, Literal

import structlog
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backend import __version__
from backend.agent.agent import Agent
from backend.agent.sessions import ConversationStore
from backend.agent.tools import ToolRegistry
from backend.api.auth import load_or_create_token, require_token
from backend.api.setup import router as setup_router
from backend.api.websocket import Services, agent_endpoint
from backend.config import get_settings
from backend.db import connect as connect_db
from backend.integrations.google_auth import GoogleAuth, GoogleNotConnected
from backend.log_setup import configure_logging
from backend.memory.store import MemoryStore
from backend.tools.calendar import Calendar, calendar_tools
from backend.tools.gmail import Gmail, gmail_tools
from backend.tools.memory import memory_tools
from backend.tools.notes import notes_tools
from backend.tools.reminders import reminders_tools
from backend.tools.system import system_tools
from backend.tools.web import web_tools
from backend.voice.gemini import GeminiLLM
from backend.voice.llm import MLXLLM, SwitchableLLM
from backend.voice.mlx_runtime import MLXWorker
from backend.voice.stt import ParakeetSTT
from backend.voice.tts import KokoroTTS, QwenTTS, SwitchableTTS

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app.state.token = load_or_create_token(settings.data_dir / "agent.token")
    # Models load lazily on first use, not at startup.
    worker = MLXWorker()
    app.state.stt = ParakeetSTT(settings.stt_model_path, worker)
    app.state.tts = SwitchableTTS(
        {
            "kokoro": KokoroTTS(settings.kokoro_model_path, worker, settings.kokoro_voice),
            "qwen": QwenTTS(settings.tts_model_path, worker, settings.tts_voice),
        },
        settings.data_dir / "preferences.json",
        default=settings.tts_engine,
    )
    app.state.llm = SwitchableLLM(
        {
            "gemini": GeminiLLM(
                settings.gemini_model,
                fallback=settings.gemini_fallback_model,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.gemini_temperature,
                thinking=settings.gemini_thinking,
            ),
            "local": MLXLLM(
                settings.llm_model_path,
                worker,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
                top_p=settings.llm_top_p,
            ),
        },
        settings.data_dir / "preferences.json",
        default=settings.llm_provider,
    )
    if not app.state.llm.available:  # e.g. Gemini chosen but no key stored
        log.warning("llm.unavailable", engine=app.state.llm.engine, fallback="local")
        app.state.llm.engine = "local"
    google = GoogleAuth(
        f"http://{settings.personal_agent_host}:{settings.personal_agent_port}/auth/google/callback",
        profile_path=settings.data_dir / "profile.json",
    )
    app.state.google = google
    db = connect_db(settings.data_dir / "database.sqlite")
    app.state.db = db
    memories = MemoryStore(db)
    tools = ToolRegistry()
    tools.register(
        *system_tools(),
        *gmail_tools(Gmail(google)),
        *calendar_tools(Calendar(google, settings.data_dir / "preferences.json")),
        *web_tools(),
        *memory_tools(memories),
        *reminders_tools(),
        *notes_tools(),
    )
    app.state.services = Services(
        agent=Agent(app.state.llm, tools, profile=google.profile, memories=memories.all),
        conversations=ConversationStore(db),
        stt=app.state.stt,
        tts=app.state.tts,
    )
    yield
    worker.shutdown()
    db.close()


app = FastAPI(title="Personal Agent", version=__version__, lifespan=lifespan)
app.include_router(setup_router)


@app.get("/health")
async def health() -> dict[str, Any]:
    # Unauthenticated on purpose: the app polls it before it has the token,
    # and it reveals nothing personal.
    state = app.state
    return {
        "status": "ok",
        "version": __version__,
        "models": {"stt": state.stt.loaded, "llm": state.llm.loaded, "tts": state.tts.loaded},
        "database": getattr(state, "db", None) is not None,
    }


@app.post("/shutdown", dependencies=[Depends(require_token)])
async def shutdown() -> dict[str, bool]:
    """Stop gracefully. The app calls this on a backend left over from an earlier
    run (e.g. when Xcode stopped the app), which may be running older code."""
    asyncio.get_running_loop().call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
    return {"ok": True}


@app.get("/models", dependencies=[Depends(require_token)])
async def models() -> dict[str, Any]:
    return {
        kind: {"name": m.name, "available": m.available, "loaded": m.loaded}
        for kind, m in (("stt", app.state.stt), ("llm", app.state.llm), ("tts", app.state.tts))
    }


class EngineChoice(BaseModel):
    engine: str


@app.get("/voice", dependencies=[Depends(require_token)])
async def voice() -> dict[str, Any]:
    tts: SwitchableTTS = app.state.tts
    return {
        "engine": tts.engine,
        "engines": [
            {"id": "kokoro", "name": "Kokoro (steady)", "available": tts.engines["kokoro"].available},
            {"id": "qwen", "name": "Qwen3-TTS (expressive)", "available": tts.engines["qwen"].available},
        ],
    }


@app.put("/voice", dependencies=[Depends(require_token)])
async def set_voice(choice: EngineChoice) -> dict[str, Any]:
    try:
        app.state.tts.select(choice.engine)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # Load it now, in the background, so the next reply doesn't wait for it.
    asyncio.create_task(app.state.tts.load())
    return await voice()


@app.get("/brain", dependencies=[Depends(require_token)])
async def brain() -> dict[str, Any]:
    llm: SwitchableLLM = app.state.llm
    return {
        "engine": llm.engine,
        "engines": [
            {"id": "gemini", "name": "Gemini Flash (cloud)", "available": llm.engines["gemini"].available},
            {"id": "local", "name": "Qwen3.5 9B (local)", "available": llm.engines["local"].available},
        ],
    }


@app.put("/brain", dependencies=[Depends(require_token)])
async def set_brain(choice: EngineChoice) -> dict[str, Any]:
    llm: SwitchableLLM = app.state.llm
    if choice.engine in llm.engines and not llm.engines[choice.engine].available:
        raise HTTPException(400, f"{choice.engine} isn't set up")
    try:
        llm.select(choice.engine)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # The local model takes a few seconds to load; start now, not on the next question.
    asyncio.create_task(app.state.services.agent.warm())
    return await brain()


@app.post("/models/{kind}/load", dependencies=[Depends(require_token)])
async def load_model(kind: Literal["stt", "llm", "tts"]) -> dict[str, Any]:
    await getattr(app.state, kind).load()
    return {"loaded": True}


@app.get("/connections", dependencies=[Depends(require_token)])
async def connections() -> dict[str, Any]:
    # Never includes credential values.
    google: GoogleAuth = app.state.google
    connected = google.connected
    return {
        "google": {
            "configured": google.configured,
            "connected": connected,
            "gmail": connected,
            "calendar": connected,
        }
    }


@app.post("/auth/google/start", dependencies=[Depends(require_token)])
async def google_start() -> dict[str, Any]:
    try:
        return {"url": app.state.google.start()}
    except GoogleNotConnected as e:
        raise HTTPException(409, str(e)) from e


@app.post("/auth/google/disconnect", dependencies=[Depends(require_token)])
async def google_disconnect() -> dict[str, Any]:
    app.state.google.disconnect()
    return {"connected": False}


@app.get("/auth/google/callback", response_class=HTMLResponse)
async def google_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    # Unauthenticated: the browser can't send the agent token. The one-time
    # `state` from /auth/google/start is what ties this request to it.
    if error:
        return _page("Google sign-in was cancelled", escape(error), 400)
    try:
        email = await app.state.google.finish(state, code)
    except GoogleNotConnected as e:
        return _page("Couldn't connect Google", escape(str(e)), 400)
    except Exception as e:
        return _page("Couldn't connect Google", escape(type(e).__name__), 500)
    return _page(
        "Google connected",
        f"Signed in as {escape(email)}. You can close this tab and go back to Personal Agent.",
    )


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = (
        f"<!doctype html><meta charset=utf-8><title>{title}</title>"
        "<body style='font:16px -apple-system,sans-serif;max-width:32em;margin:4em auto;color:#222'>"
        f"<h2>{title}</h2><p>{body}</p></body>"
    )
    return HTMLResponse(html, status_code=status)


# One session type serves both: typed turns get text replies, spoken turns
# get text plus audio.
app.add_api_websocket_route("/ws/chat", agent_endpoint)
app.add_api_websocket_route("/ws/voice", agent_endpoint)
