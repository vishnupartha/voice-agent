"""Conversation state, saved to SQLite so it survives restarts (spec §47, §74)."""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field

import orjson

from backend.voice.llm import Message

MAX_HISTORY_MESSAGES = 40
# Opening the mic after this long without a turn starts a new conversation.
IDLE_NEW_CONVERSATION_S = 5 * 60


def _trim(messages: list[Message]) -> None:
    """Crude cap until conversation summarization lands (spec §78). Cut at a
    user message so no tool result is left without the call it answers."""
    cut = max(0, len(messages) - MAX_HISTORY_MESSAGES)
    while cut < len(messages) and messages[cut]["role"] != "user":
        cut += 1
    del messages[:cut]


@dataclass
class Conversation:
    id: str
    # The recent messages the model sees; the database keeps all of them.
    messages: list[Message] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    db: sqlite3.Connection | None = None
    _seq: int = 0  # messages stored so far, including ones trimmed away

    def add(self, message: Message) -> None:
        self.messages.append(message)
        _trim(self.messages)
        self.updated_at = time.time()
        if self.db is not None:
            # The conversation's row is written with its first message, so
            # sessions that never say anything leave nothing behind.
            self.db.execute(
                "INSERT INTO conversations (id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
                (self.id, self.updated_at, self.updated_at),
            )
            self.db.execute(
                "INSERT INTO messages (conversation_id, seq, role, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.id, self._seq, message["role"], orjson.dumps(message).decode(), self.updated_at),
            )
        self._seq += 1

    @property
    def idle(self) -> bool:
        """Started, but nothing said for a while: time for a fresh one."""
        return bool(self.messages) and time.time() - self.updated_at > IDLE_NEW_CONVERSATION_S


class ConversationStore:
    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self.db = db
        self._conversations: dict[str, Conversation] = {}

    def get_or_create(self, conversation_id: str | None) -> Conversation:
        cid = conversation_id or uuid.uuid4().hex
        if cid not in self._conversations:
            self._conversations[cid] = self._load(cid) or Conversation(id=cid, db=self.db)
        return self._conversations[cid]

    def new(self) -> Conversation:
        return self.get_or_create(None)

    def _load(self, cid: str) -> Conversation | None:
        if self.db is None:
            return None
        row = self.db.execute("SELECT updated_at FROM conversations WHERE id = ?", (cid,)).fetchone()
        if row is None:
            return None
        recent = self.db.execute(
            "SELECT seq, body FROM messages WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?",
            (cid, MAX_HISTORY_MESSAGES),
        ).fetchall()
        messages = [orjson.loads(r["body"]) for r in reversed(recent)]
        _trim(messages)
        seq = recent[0]["seq"] + 1 if recent else 0
        return Conversation(cid, messages, row["updated_at"], self.db, seq)
