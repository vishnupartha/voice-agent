"""WebSocket protocol. Mirrored in MacApp/Agent/MessageTypes.swift — keep in sync."""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

# ---- client -> backend ----


class SessionStart(BaseModel):
    type: Literal["session.start"]
    session_id: str
    conversation_id: str | None = None


class UserMessage(BaseModel):
    type: Literal["user.message"]
    text: str = Field(min_length=1, max_length=20_000)


class AudioStart(BaseModel):
    type: Literal["audio.start"]


class AudioStop(BaseModel):
    type: Literal["audio.stop"]


class ConversationCancel(BaseModel):
    type: Literal["conversation.cancel"]


class ApprovalResponse(BaseModel):
    type: Literal["approval.response"]
    approval_id: str
    approved: bool


class BridgeResponse(BaseModel):
    """The Mac app's answer to a bridge.request."""

    type: Literal["bridge.response"]
    request_id: str
    ok: bool
    result: Any = None
    error: str | None = None


ClientMessage = Annotated[
    SessionStart
    | UserMessage
    | AudioStart
    | AudioStop
    | ConversationCancel
    | ApprovalResponse
    | BridgeResponse,
    Field(discriminator="type"),
]
client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# ---- backend -> client ----

EventType = Literal[
    "session.ready",
    "input.speech_started",
    "transcript.partial",
    "transcript.final",
    "agent.thinking",
    "tool.started",
    "tool.completed",
    "assistant.text.delta",
    "assistant.text.final",
    "assistant.audio.start",
    "assistant.audio.end",
    "approval.required",
    "approval.resolved",
    "draft.updated",
    "system.open_url",
    "bridge.request",
    "error",
]


def event(type: EventType, session_id: str, **data: Any) -> dict[str, Any]:
    return {
        "type": type,
        "session_id": session_id,
        "event_id": uuid.uuid4().hex,
        "timestamp": time.time(),
        **data,
    }
