"""Conversation state. In-memory for Stage 0; SQLite-backed in Stage 2."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from backend.voice.llm import Message

MAX_HISTORY_MESSAGES = 40


@dataclass
class Conversation:
    id: str
    messages: list[Message] = field(default_factory=list)

    def add(self, message: Message) -> None:
        self.messages.append(message)
        # Crude cap until conversation summarization lands (spec §78). Cut at a
        # user message so no tool result is left without the call it answers.
        if len(self.messages) > MAX_HISTORY_MESSAGES:
            cut = len(self.messages) - MAX_HISTORY_MESSAGES
            while cut < len(self.messages) and self.messages[cut]["role"] != "user":
                cut += 1
            del self.messages[:cut]


class ConversationStore:
    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}

    def get_or_create(self, conversation_id: str | None) -> Conversation:
        cid = conversation_id or uuid.uuid4().hex
        if cid not in self._conversations:
            self._conversations[cid] = Conversation(id=cid)
        return self._conversations[cid]
