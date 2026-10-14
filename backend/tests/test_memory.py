from __future__ import annotations

import threading
import time

import pytest

from backend.agent.prompts import system_prompt
from backend.agent.sessions import IDLE_NEW_CONVERSATION_S, MAX_HISTORY_MESSAGES, ConversationStore
from backend.agent.tools import ToolCall, ToolContext, ToolRegistry
from backend.db import connect
from backend.memory.store import MemoryStore
from backend.tools.memory import memory_tools


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    yield conn
    conn.close()


async def _emit(*_, **__) -> None:
    pass


async def _deny(_) -> bool:
    return False


def test_migrations_run_once(tmp_path) -> None:
    connect(tmp_path / "db.sqlite").close()
    conn = connect(tmp_path / "db.sqlite")  # reopening must not re-create tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_conversation_survives_restart(db) -> None:
    conv = ConversationStore(db).new()
    conv.add({"role": "user", "content": "hi"})
    conv.add(
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x", "arguments": {}}}]}
    )
    conv.add({"role": "tool", "content": "{}"})

    again = ConversationStore(db).get_or_create(conv.id)
    assert again.messages == conv.messages
    again.add({"role": "assistant", "content": "done"})  # continues the sequence
    assert len(ConversationStore(db).get_or_create(conv.id).messages) == 4


def test_reload_starts_at_a_user_message(db) -> None:
    conv = ConversationStore(db).new()
    for i in range(MAX_HISTORY_MESSAGES):
        conv.add({"role": "user", "content": str(i)})
        conv.add({"role": "tool", "content": "{}"})
    loaded = ConversationStore(db).get_or_create(conv.id).messages
    assert loaded[0]["role"] == "user" and len(loaded) <= MAX_HISTORY_MESSAGES


def test_empty_conversations_are_not_saved(db) -> None:
    ConversationStore(db).new()
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0


def test_idle_after_five_minutes(db) -> None:
    conv = ConversationStore(db).new()
    assert not conv.idle  # nothing said yet
    conv.add({"role": "user", "content": "hi"})
    assert not conv.idle
    conv.updated_at = time.time() - IDLE_NEW_CONVERSATION_S - 1
    assert conv.idle


async def test_remember_and_forget(db) -> None:
    store = MemoryStore(db)
    tools = ToolRegistry()
    tools.register(*memory_tools(store))
    ctx = ToolContext(_emit, _deny, threading.Event())

    r = await tools.run(ToolCall("memory.remember", {"text": "The user is vegetarian."}), ctx)
    assert r.ok and store.all() == [(1, "The user is vegetarian.")]
    assert "[1] The user is vegetarian." in system_prompt(memories=store.all())

    assert (await tools.run(ToolCall("memory.forget", {"id": "1"}), ctx)).ok
    assert store.all() == []
    assert not (await tools.run(ToolCall("memory.forget", {"id": "1"}), ctx)).ok
