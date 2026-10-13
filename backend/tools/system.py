"""system.* tools (spec §57)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.agent.tools import Permission, Tool, ToolContext


class NoArgs(BaseModel):
    pass


class UrlArgs(BaseModel):
    url: str = Field(pattern=r"^https?://")


def system_tools() -> list[Tool]:
    async def get_time(a: NoArgs, ctx: ToolContext) -> Any:
        now = datetime.now().astimezone()
        return {
            "now": now.isoformat(timespec="minutes"),
            "weekday": now.strftime("%A"),
            "timezone": now.tzname(),
        }

    async def open_url(a: UrlArgs, ctx: ToolContext) -> Any:
        await ctx.open_url(a.url)
        return {"opened": True}

    return [
        Tool("system.get_time", "Current local date, time and timezone.", NoArgs, get_time, Permission.READ,
             label="Checked the time"),
        Tool("system.open_url", "Open a web page in the user's browser.", UrlArgs, open_url, Permission.READ,
             label="Opened a page"),
    ]  # fmt: skip
