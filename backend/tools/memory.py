"""memory.* tools. Everything remembered is already in the
system prompt, so there is no tool to look memories up."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from backend.agent.tools import Permission, Tool, ToolContext, ToolError
from backend.memory.store import MemoryStore


class RememberArgs(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ForgetArgs(BaseModel):
    id: int


def memory_tools(store: MemoryStore) -> list[Tool]:
    async def remember(a: RememberArgs, ctx: ToolContext) -> Any:
        return {"remembered": True, "id": store.add(a.text.strip())}

    async def forget(a: ForgetArgs, ctx: ToolContext) -> Any:
        if (text := store.remove(a.id)) is None:
            raise ToolError(f"no memory with id {a.id}")
        return {"forgotten": text}

    return [
        Tool("memory.remember", "Save a fact about the user for future conversations, written in the third "
             "person (\"The user's advisor is Dr. Lee\"). Only when the user explicitly asks you to remember it.",
             RememberArgs, remember, Permission.WRITE, label="Remembered that"),
        Tool("memory.forget", "Delete a saved memory by its ID (listed under \"What you remember\").",
             ForgetArgs, forget, Permission.WRITE, label="Forgot that"),
    ]  # fmt: skip
