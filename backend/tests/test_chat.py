from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator

import orjson
import pytest
from fastapi.testclient import TestClient

from backend.agent.agent import Agent
from backend.api.auth import TOKEN_HEADER
from backend.api.server import app
from backend.config import Settings, get_settings
from backend.voice.llm import GenerationStats, Message


class FakeLLM:
    name = "fake"
    loaded = True

    def __init__(self, tokens: list[str], delay: float = 0.0) -> None:
        self.tokens = tokens
        self.delay = delay
        self.seen: list[list[Message]] = []

    async def load(self) -> None:
        pass

    async def stream(
        self,
        messages: list[Message],
        cancel: threading.Event,
        stats: GenerationStats,
        tools: list | None = None,
        prefix: str = "",
    ) -> AsyncIterator[str]:
        self.seen.append(list(messages))
        for t in self.tokens:
            if cancel.is_set():
                stats.cancelled = True
                return
            await asyncio.sleep(self.delay)
            stats.tokens += 1
            yield t


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, model_dir=tmp_path / "models", log_dir=tmp_path / "logs")
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr("backend.api.server.get_settings", lambda: settings)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def use_llm(llm: FakeLLM) -> None:
    app.state.services.agent = Agent(llm)


def recv_until(ws, type_: str) -> list[dict]:
    events = []
    while True:
        e = orjson.loads(ws.receive_text())
        events.append(e)
        if e["type"] in (type_, "error"):
            return events


def test_health_is_public(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_token_required(client: TestClient) -> None:
    assert client.get("/models").status_code == 401
    assert client.get("/models", headers={TOKEN_HEADER: app.state.token}).status_code == 200


def test_ws_rejects_missing_token(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat") as ws:
            ws.receive_text()


def test_chat_streams_and_keeps_history(client: TestClient) -> None:
    llm = FakeLLM(["Hel", "lo", "!"])
    use_llm(llm)
    with client.websocket_connect("/ws/chat", headers={TOKEN_HEADER: app.state.token}) as ws:
        ws.send_text(orjson.dumps({"type": "session.start", "session_id": "s1"}).decode())
        ready = recv_until(ws, "session.ready")[-1]
        assert ready["session_id"] == "s1" and ready["event_id"] and ready["timestamp"]

        ws.send_text(orjson.dumps({"type": "user.message", "text": "hi"}).decode())
        events = recv_until(ws, "assistant.text.final")
        assert [e["delta"] for e in events if e["type"] == "assistant.text.delta"] == ["Hel", "lo", "!"]
        assert events[-1]["text"] == "Hello!"

        ws.send_text(orjson.dumps({"type": "user.message", "text": "again"}).decode())
        recv_until(ws, "assistant.text.final")
        roles = [m["role"] for m in llm.seen[-1]]
        assert roles == ["system", "user", "assistant", "user"]


def test_cancel_stops_generation(client: TestClient) -> None:
    use_llm(FakeLLM(["x"] * 200, delay=0.01))
    with client.websocket_connect("/ws/chat", headers={TOKEN_HEADER: app.state.token}) as ws:
        ws.send_text(orjson.dumps({"type": "session.start", "session_id": "s"}).decode())
        recv_until(ws, "session.ready")
        ws.send_text(orjson.dumps({"type": "user.message", "text": "go"}).decode())
        recv_until(ws, "assistant.text.delta")
        ws.send_text(orjson.dumps({"type": "conversation.cancel"}).decode())
        final = recv_until(ws, "assistant.text.final")[-1]
        assert final["cancelled"] is True
        assert len(final["text"]) < 200


def test_bad_message(client: TestClient) -> None:
    with client.websocket_connect("/ws/chat", headers={TOKEN_HEADER: app.state.token}) as ws:
        ws.send_text('{"type": "nope"}')
        assert orjson.loads(ws.receive_text())["code"] == "bad_message"
