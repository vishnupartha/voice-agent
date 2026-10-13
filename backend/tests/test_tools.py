from __future__ import annotations

import asyncio
import base64
import threading
from collections.abc import AsyncIterator
from datetime import datetime, time, timedelta

import orjson
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.agent.agent import Agent
from backend.agent.toolcalls import ReplyFilter
from backend.agent.tools import ApprovalRequest, Permission, Tool, ToolCall, ToolContext, ToolRegistry
from backend.api.auth import TOKEN_HEADER
from backend.api.server import app
from backend.api.websocket import spoken_decision
from backend.config import Settings
from backend.tools.calendar import free_slots
from backend.tools.gmail import body_text
from backend.voice.llm import GenerationStats, Message


def call_markup(name: str, **params: str) -> str:
    body = "".join(f"<parameter={k}>\n{v}\n</parameter>\n" for k, v in params.items())
    return f"<tool_call>\n<function={name}>\n{body}</function>\n</tool_call>"


class ScriptedLLM:
    """Replies with the next scripted string each step, streamed in small pieces."""

    name = "scripted"
    loaded = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[list[Message]] = []
        self.tools_seen: list[list | None] = []

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
        self.tools_seen.append(tools)
        reply = self.replies.pop(0)
        for i in range(0, len(reply), 3):
            stats.tokens += 1
            yield reply[i : i + 3]


class EchoArgs(BaseModel):
    text: str
    count: int = 1


class IdArgs(BaseModel):
    draft_id: str


def make_registry(log: list[str]) -> ToolRegistry:
    async def echo(a: EchoArgs, ctx: ToolContext) -> dict:
        log.append(f"echo:{a.text}:{a.count}")
        await asyncio.sleep(0.05)
        return {"echo": a.text * a.count}

    async def send(a: IdArgs, ctx: ToolContext) -> dict:
        ok = await ctx.request_approval(ApprovalRequest("test.send", "Send Email", {"To": "x@y.z"}))
        log.append(f"send:{a.draft_id}:{ok}")
        return {"status": "sent" if ok else "not sent"}

    async def nope(a: EchoArgs, ctx: ToolContext) -> dict:
        raise AssertionError("must never run")

    reg = ToolRegistry()
    reg.register(
        Tool("test.echo", "Echo.", EchoArgs, echo, Permission.READ, untrusted=True, label="Echoed"),
        Tool("test.send", "Send.", IdArgs, send, Permission.APPROVAL, timeout_s=30),
        Tool("test.forbidden", "No.", EchoArgs, nope, Permission.FORBIDDEN),
    )
    return reg


# ---- parsing ----


def test_reply_filter_hides_tool_calls_while_streaming() -> None:
    raw = "Let me check. " + call_markup("gmail.search", query="from:prof", max_results="5")
    f = ReplyFilter()
    shown = "".join(f.feed(raw[i : i + 2]) for i in range(0, len(raw), 2))
    assert shown == "Let me check. "
    text, calls = f.finish()
    assert text == "Let me check."
    assert calls[0].name == "gmail.search"
    assert calls[0].arguments == {"query": "from:prof", "max_results": "5"}


def test_reply_filter_passes_plain_text_and_lone_angle_brackets() -> None:
    f = ReplyFilter()
    shown = "".join(f.feed(c) for c in "If x < 3 then <b>.")
    text, calls = f.finish()
    assert calls == [] and text == "If x < 3 then <b>."
    assert shown == "If x < 3 then <b>."[: len(shown)]


async def test_registry_coerces_by_schema_and_reports_errors() -> None:
    log: list[str] = []
    reg = make_registry(log)
    ctx = ToolContext(lambda *a, **k: asyncio.sleep(0), lambda r: asyncio.sleep(0, False), threading.Event())

    ok = await reg.run(ToolCall("test.echo", {"text": "1849", "count": "2"}), ctx)
    assert ok.ok and log == ["echo:1849:2"]  # "1849" stays a string, "2" becomes an int
    assert ok.content.startswith("UNTRUSTED CONTENT")

    bad = await reg.run(ToolCall("test.echo", {"count": "x"}), ctx)
    assert not bad.ok and "invalid arguments" in bad.content

    assert "not allowed" in (await reg.run(ToolCall("test.forbidden", {"text": "a"}), ctx)).content
    assert "unknown tool" in (await reg.run(ToolCall("nope", {}), ctx)).content
    assert "test.forbidden" not in orjson.dumps(reg.schemas()).decode()


# ---- agent loop ----


async def test_agent_runs_parallel_reads_then_answers() -> None:
    log: list[str] = []
    llm = ScriptedLLM(
        "One moment. " + call_markup("test.echo", text="a") + call_markup("test.echo", text="b"),
        "Both came back fine.",
    )
    agent = Agent(llm, make_registry(log))
    events: list[tuple[str, dict]] = []

    async def emit(type, **data):
        events.append((type, data))

    from backend.agent.sessions import Conversation

    conv = Conversation("c")
    t0 = asyncio.get_running_loop().time()
    await agent.run_turn(conv, "check", emit, threading.Event())
    elapsed = asyncio.get_running_loop().time() - t0

    assert sorted(log) == ["echo:a:1", "echo:b:1"]
    assert elapsed < 0.09  # two 50 ms reads ran concurrently
    deltas = "".join(d["delta"] for t, d in events if t == "assistant.text.delta")
    assert deltas == "One moment. \nBoth came back fine."
    final = events[-1][1]
    assert final["text"] == "One moment.\nBoth came back fine."
    assert [a["tool"] for a in final["tools"]] == ["test.echo", "test.echo"]
    assert [m["role"] for m in conv.messages] == ["user", "assistant", "tool", "tool", "assistant"]
    # Second step's prompt included the tool results.
    assert any("UNTRUSTED CONTENT" in m["content"] for m in llm.seen[1] if m["role"] == "tool")


async def test_agent_stops_offering_tools_on_last_step() -> None:
    from backend.agent import agent as agent_mod
    from backend.agent.sessions import Conversation

    loops = [call_markup("test.echo", text="x")] * (agent_mod.MAX_STEPS - 1) + ["Done."]
    llm = ScriptedLLM(*loops)
    await Agent(llm, make_registry([])).run_turn(
        Conversation("c"), "go", lambda *a, **k: asyncio.sleep(0), threading.Event()
    )
    assert llm.tools_seen[-1] is None and all(t for t in llm.tools_seen[:-1])


# ---- approvals over the websocket ----


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, model_dir=tmp_path / "models", log_dir=tmp_path / "logs")
    monkeypatch.setattr("backend.api.server.get_settings", lambda: settings)
    with TestClient(app) as c:
        yield c


def recv_until(ws, type_: str) -> list[dict]:
    out = []
    while True:
        msg = ws.receive()
        if msg.get("text") is None:
            continue
        e = orjson.loads(msg["text"])
        out.append(e)
        if e["type"] in (type_, "error"):
            return out


def run_send_turn(client, log, answer) -> list[dict]:
    llm = ScriptedLLM(call_markup("test.send", draft_id="d1"), "Okay.")
    app.state.services.agent = Agent(llm, make_registry(log))
    with client.websocket_connect("/ws/chat", headers={TOKEN_HEADER: app.state.token}) as ws:
        ws.send_text(orjson.dumps({"type": "session.start", "session_id": "s"}).decode())
        recv_until(ws, "session.ready")
        ws.send_text(orjson.dumps({"type": "user.message", "text": "send it"}).decode())
        req = recv_until(ws, "approval.required")[-1]
        assert req["title"] == "Send Email" and req["fields"] == [{"label": "To", "value": "x@y.z"}]
        answer(ws, req["approval_id"])
        return recv_until(ws, "assistant.text.final")


def test_approval_granted_runs_action(client) -> None:
    log: list[str] = []

    def approve(ws, approval_id):
        ws.send_text(
            orjson.dumps({"type": "approval.response", "approval_id": approval_id, "approved": True}).decode()
        )

    events = run_send_turn(client, log, approve)
    assert log == ["send:d1:True"]
    assert any(e["type"] == "approval.resolved" and e["approved"] for e in events)


def test_approval_for_other_id_does_nothing_then_cancel_declines(client) -> None:
    log: list[str] = []

    def wrong_then_cancel(ws, approval_id):
        ws.send_text(
            orjson.dumps({"type": "approval.response", "approval_id": "nope", "approved": True}).decode()
        )
        ws.send_text(orjson.dumps({"type": "conversation.cancel"}).decode())

    run_send_turn(client, log, wrong_then_cancel)
    assert log == ["send:d1:False"]


@pytest.mark.parametrize(
    ("text", "decision"),
    [
        ("Send it.", True),
        ("Yeah, go ahead", True),
        ("Okay", True),
        ("No, wait.", False),
        ("Cancel that", False),
        ("Don't send it", False),
        ("Make it shorter", None),
        ("Yes but first change the subject line to something about Thursday", None),
    ],
)
def test_spoken_decision(text: str, decision: bool | None) -> None:
    assert spoken_decision(text) is decision


# ---- calendar / gmail helpers ----


def test_free_slots() -> None:
    tz = datetime.now().astimezone().tzinfo
    day = datetime(2026, 10, 15, tzinfo=tz)
    busy = [(day.replace(hour=10), day.replace(hour=11)), (day.replace(hour=13), day.replace(hour=15))]
    slots = free_slots(day, day + timedelta(hours=23, minutes=59), busy, 30, time(9), time(18))
    assert [(a.hour, b.hour) for a, b in slots] == [(9, 10), (11, 13), (15, 18)]


def test_body_text_prefers_plain_and_drops_quotes() -> None:
    def part(mime: str, text: str) -> dict:
        return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            part("text/plain", "Can we meet Thursday?\n\nOn Mon, Oct 12, Bob wrote:\n> old stuff"),
            part("text/html", "<p>Can we meet Thursday?</p>"),
        ],
    }
    assert body_text(payload) == "Can we meet Thursday?"
    assert body_text({"mimeType": "text/html", **part("text/html", "<p>Hi <b>there</b></p>")}) == "Hi \nthere"
