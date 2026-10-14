"""Apple Reminders tools. The Mac app does the work with EventKit (see
MacApp/System/Reminders.swift), reached over the bridge. Creating and
completing run freely; deleting asks first."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.agent.tools import ApprovalRequest, Permission, Tool, ToolContext
from backend.tools.calendar import parse_when


class ListArgs(BaseModel):
    list: str | None = Field(None, description="Reminders list name; default: all lists")
    due_before: str | None = Field(
        None, description="ISO date/time; only reminders due by then (overdue too)"
    )


class CreateArgs(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    due: str | None = Field(None, description="ISO date (2026-10-15) or date-time (2026-10-15T18:00)")
    list: str | None = Field(None, description="Reminders list name; default: the user's default list")
    notes: str | None = Field(None, description="Extra details shown under the reminder (not Apple Notes)")


class IdArgs(BaseModel):
    id: str


def _when(value: str) -> dict[str, Any]:
    """What the app needs to set a due date: the moment, and whether it has a time of day."""
    return {"due": parse_when(value).timestamp(), "timed": "T" in value or " " in value.strip()}


def _describe(r: dict[str, Any]) -> dict[str, Any]:
    """The app's reminder, with its due date readable."""
    out = {k: v for k, v in r.items() if k not in ("due", "timed") and v not in (None, "")}
    if (due := r.get("due")) is not None:
        dt = datetime.fromtimestamp(due).astimezone()
        out["due"] = dt.strftime("%a %b %-d, %-I:%M %p" if r.get("timed") else "%a %b %-d")
    return out


def reminders_tools() -> list[Tool]:
    async def list_reminders(a: ListArgs, ctx: ToolContext) -> Any:
        params: dict[str, Any] = {"list": a.list}
        if a.due_before:
            params["due_before"] = parse_when(a.due_before).timestamp()
        res = await ctx.mac("reminders.list", **params)
        return {"lists": res["lists"], "reminders": [_describe(r) for r in res["reminders"]]}

    async def create(a: CreateArgs, ctx: ToolContext) -> Any:
        params: dict[str, Any] = {"title": a.title, "list": a.list, "notes": a.notes}
        if a.due:
            params |= _when(a.due)
        return {"status": "created", **_describe(await ctx.mac("reminders.create", **params))}

    async def complete(a: IdArgs, ctx: ToolContext) -> Any:
        return {"status": "completed", **_describe(await ctx.mac("reminders.complete", id=a.id))}

    async def delete(a: IdArgs, ctx: ToolContext) -> Any:
        current = _describe(await ctx.mac("reminders.get", id=a.id))
        details = {"Reminder": current["title"], "Due": current.get("due"), "List": current.get("list")}
        if not await ctx.request_approval(ApprovalRequest("reminders.delete", "Delete Reminder", details)):
            return {"status": "not deleted: the user declined or didn't confirm"}
        await ctx.mac("reminders.delete", id=a.id)
        return {"status": "deleted", "title": current["title"]}

    W = Permission.WRITE
    return [
        Tool("reminders.list", "List the user's open (not completed) Apple Reminders, soonest due first, "
             "and the names of their lists.", ListArgs, list_reminders, Permission.READ, timeout_s=70,
             untrusted=True, label="Checked reminders"),
        Tool("reminders.create", "Add an Apple Reminder (\"remind me to…\"). With a time, it alerts then. "
             "Runs without asking; tell the user what you added.", CreateArgs, create, W, timeout_s=70,
             label="Added reminder"),
        Tool("reminders.complete", "Mark a reminder done, by its id from reminders.list.", IdArgs, complete, W,
             timeout_s=70, label="Completed reminder"),
        Tool("reminders.delete", "Delete a reminder, by its id from reminders.list. The user is asked to "
             "approve first.", IdArgs, delete, Permission.APPROVAL, timeout_s=180, label="Deleted reminder"),
    ]  # fmt: skip
