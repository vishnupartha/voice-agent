"""Google Calendar tools (spec §27–28). Reads run freely; creating, changing
and deleting events go through approval."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import structlog
from pydantic import BaseModel, Field

from backend.agent.tools import ApprovalRequest, Permission, Tool, ToolContext, ToolError
from backend.integrations.google_auth import GoogleAuth, GoogleNotConnected
from backend.preferences import read_preferences

log = structlog.get_logger(__name__)


def local_tz() -> Any:
    return datetime.now().astimezone().tzinfo


def parse_when(value: str) -> datetime:
    """ISO date or datetime; naive values are taken as local time."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ToolError(
            f"couldn't read {value!r} as a date/time; use ISO format like 2026-10-14T15:00"
        ) from e
    return dt if dt.tzinfo else dt.replace(tzinfo=local_tz())


def _fmt(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %b %-d, %-I:%M %p")


CALENDAR_LIST_TTL_S = 600


class Calendar:
    def __init__(self, auth: GoogleAuth, preferences: Path | None = None) -> None:
        self.auth = auth
        self._prefs = preferences
        self._calendars: list[dict[str, Any]] = []
        self._calendars_at = 0.0

    def preference(self, key: str) -> Any:
        return read_preferences(self._prefs).get(key) if self._prefs else None

    async def calendars(self) -> list[dict[str, Any]]:
        """Every calendar in the user's list (own, shared, subscribed), cached briefly."""
        if not self._calendars or monotonic() - self._calendars_at > CALENDAR_LIST_TTL_S:
            # Calendars hidden in the app (e.g. someone else's, shared with the
            # user): {"id", "name"} entries under "hidden_calendars".
            hidden = self.preference("hidden_calendars") or []
            hidden_ids = {h["id"] for h in hidden}
            resp = await self.call(lambda svc: svc.calendarList().list(maxResults=250))
            self._calendars = [
                {
                    "id": c["id"],
                    "name": c.get("summaryOverride") or c.get("summary", c["id"]),
                    "primary": bool(c.get("primary")),
                    # Ticked in Google Calendar's sidebar, i.e. ones the user looks at.
                    "shown": bool(c.get("selected")) or bool(c.get("primary")),
                    "can_edit": c.get("accessRole") in ("owner", "writer"),
                }
                for c in resp.get("items", [])
                if not c.get("deleted") and c["id"] not in hidden_ids
            ]
            self._calendars_at = monotonic()
        return self._calendars

    async def resolve(self, calendar_id: str | None) -> list[dict[str, Any]]:
        """The calendars a read covers: one by ID or name, or all shown ones."""
        cals = await self.calendars()
        if not calendar_id:
            return [c for c in cals if c["shown"]]
        if calendar_id == "primary":
            return [c for c in cals if c["primary"]] or [{"id": "primary", "name": "primary"}]
        wanted = calendar_id.casefold()
        match = [c for c in cals if c["id"] == calendar_id or c["name"].casefold() == wanted]
        if not match:
            raise ToolError(f"no calendar called {calendar_id!r}; use calendar.list_calendars")
        return match

    async def events(self, calendar_id: str | None, **params: Any) -> list[dict[str, Any]]:
        """Events from each calendar `calendar_id` covers, merged in time order.
        A calendar that fails to load is skipped rather than failing the lot."""
        cals = await self.resolve(calendar_id)

        async def one(c: dict[str, Any]) -> list[dict[str, Any]]:
            try:
                resp = await self.call(
                    lambda svc: svc.events().list(
                        calendarId=c["id"], singleEvents=True, orderBy="startTime", **params
                    )
                )
            except ToolError:
                raise
            except Exception:
                log.warning("calendar.list_failed", calendar=c["name"], exc_info=True)
                return []
            return [normalize(ev, c["id"], c["name"]) for ev in resp.get("items", [])]

        merged = [ev for evs in await asyncio.gather(*(one(c) for c in cals)) for ev in evs]
        return sorted(merged, key=_start_key)

    async def call(self, fn: Any) -> Any:
        try:
            creds = await self.auth.credentials()
        except GoogleNotConnected as e:
            raise ToolError(str(e)) from e

        def run() -> Any:
            from googleapiclient.discovery import build

            return fn(build("calendar", "v3", credentials=creds, cache_discovery=False)).execute()

        return await asyncio.to_thread(run)


def _start_key(ev: dict[str, Any]) -> datetime:
    if ev["all_day"]:
        return datetime.combine(date.fromisoformat(ev["start"]), time.min, local_tz())
    return datetime.fromisoformat(ev["start_iso"])


def normalize(
    e: dict[str, Any], calendar_id: str = "primary", calendar_name: str | None = None
) -> dict[str, Any]:
    start, end = e.get("start", {}), e.get("end", {})
    all_day = "date" in start
    if all_day:
        when = {"start": start["date"], "end": end.get("date"), "all_day": True}
    else:
        when = {
            "start": _fmt(datetime.fromisoformat(start["dateTime"])),
            "end": _fmt(datetime.fromisoformat(end["dateTime"])),
            "start_iso": start["dateTime"],
            "end_iso": end["dateTime"],
            "all_day": False,
        }
    return {
        "id": e["id"],
        "calendar_id": calendar_id,
        "calendar": calendar_name or calendar_id,
        "title": e.get("summary", "(no title)"),
        **when,
        "location": e.get("location"),
        "description": (e.get("description") or "")[:500] or None,
        "organizer": e.get("organizer", {}).get("email"),
        "attendees": [a.get("email") for a in e.get("attendees", [])][:15],
        "url": e.get("htmlLink"),
    }


# ---- arguments ----


_ALL = "Calendar ID or name; default: all the user's calendars"
_ONE = "The event's calendar_id, as returned with the event"


class NoArgs(BaseModel):
    pass


class RangeArgs(BaseModel):
    start: str | None = Field(None, description="ISO date/datetime; default: start of today")
    end: str | None = Field(None, description="ISO date/datetime; default: end of the start day")
    calendar_id: str | None = Field(None, description=_ALL)
    max_results: int = Field(40, ge=1, le=100)


class EventArgs(BaseModel):
    event_id: str
    calendar_id: str = Field("primary", description=_ONE)


class SearchArgs(BaseModel):
    query: str
    start: str | None = Field(None, description="ISO; default: now")
    end: str | None = Field(None, description="ISO; default: 60 days after start")
    calendar_id: str | None = Field(None, description=_ALL)


class FreeTimeArgs(BaseModel):
    start: str = Field(description="ISO date or datetime where the search begins")
    end: str | None = Field(None, description="ISO; default: end of the start day")
    min_minutes: int = Field(30, ge=5, le=600)
    day_start: str = Field("08:00", description="Earliest time of day to count as free, HH:MM")
    day_end: str = Field("22:00", description="Latest time of day to count as free, HH:MM")


class CreateArgs(BaseModel):
    title: str
    start: str = Field(description="ISO datetime, or ISO date for an all-day event")
    end: str | None = Field(
        None, description="ISO; default: one hour after start (or the same day if all-day)"
    )
    location: str | None = None
    description: str | None = None
    attendees: list[str] | None = Field(None, description="Email addresses to invite")
    calendar_id: str = Field("primary", description="Calendar to add it to (ID or name); default: main")


class UpdateArgs(BaseModel):
    event_id: str
    title: str | None = None
    start: str | None = None
    end: str | None = None
    location: str | None = None
    description: str | None = None
    calendar_id: str = Field("primary", description=_ONE)


def _range(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    s = parse_when(start) if start else datetime.combine(date.today(), time.min, local_tz())
    if end:
        e = parse_when(end)
        if "T" not in end:  # a bare end date means through the end of that day
            e = datetime.combine(e.date(), time.max, e.tzinfo)
    else:
        e = datetime.combine(s.date(), time.max, s.tzinfo)
    return s, e


def _event_body(title: str | None, start: str | None, end: str | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    if title is not None:
        body["summary"] = title
    if start is not None:
        if "T" not in start:  # all-day
            s = date.fromisoformat(start)
            e = date.fromisoformat(end) if end else s + timedelta(days=1)
            body["start"], body["end"] = {"date": s.isoformat()}, {"date": e.isoformat()}
        else:
            s_dt = parse_when(start)
            e_dt = parse_when(end) if end else s_dt + timedelta(hours=1)
            # Offsets are in the ISO strings; Google wants IANA names for
            # "timeZone", which Python's local tzinfo doesn't provide.
            body["start"] = {"dateTime": s_dt.isoformat()}
            body["end"] = {"dateTime": e_dt.isoformat()}
    elif end is not None:
        body["end"] = {"dateTime": parse_when(end).isoformat()}
    return body


def _when_text(body: dict[str, Any]) -> str:
    s, e = body.get("start", {}), body.get("end", {})
    if "date" in s:
        return f"{s['date']} (all day)"
    if "dateTime" in s:
        return (
            f"{_fmt(datetime.fromisoformat(s['dateTime']))} – {_fmt(datetime.fromisoformat(e['dateTime']))}"
        )
    return ""


def calendar_tools(cal: Calendar) -> list[Tool]:
    async def list_calendars(a: NoArgs, ctx: ToolContext) -> Any:
        return {
            "calendars": [
                {k: c[k] for k in ("id", "name", "primary", "can_edit")} for c in await cal.calendars()
            ]
        }

    async def get_events(a: RangeArgs, ctx: ToolContext) -> Any:
        s, e = _range(a.start, a.end)
        events = await cal.events(
            a.calendar_id, timeMin=s.isoformat(), timeMax=e.isoformat(), maxResults=a.max_results
        )
        return {"from": _fmt(s), "to": _fmt(e), "count": len(events), "events": events[: a.max_results]}

    async def get_event(a: EventArgs, ctx: ToolContext) -> Any:
        ev = await cal.call(lambda svc: svc.events().get(calendarId=a.calendar_id, eventId=a.event_id))
        return normalize(ev, a.calendar_id)

    async def search(a: SearchArgs, ctx: ToolContext) -> Any:
        s = parse_when(a.start) if a.start else datetime.now().astimezone()
        e = parse_when(a.end) if a.end else s + timedelta(days=60)
        events = await cal.events(
            a.calendar_id, q=a.query, timeMin=s.isoformat(), timeMax=e.isoformat(), maxResults=25
        )
        return {"query": a.query, "count": len(events), "events": events[:25]}

    async def find_free_time(a: FreeTimeArgs, ctx: ToolContext) -> Any:
        s, e = _range(a.start, a.end)
        ids = [c["id"] for c in await cal.resolve(None)]
        resp = await cal.call(
            lambda svc: svc.freebusy().query(
                body={"timeMin": s.isoformat(), "timeMax": e.isoformat(), "items": [{"id": i} for i in ids]}
            )
        )
        # Busy time on any of the user's calendars (classes, clubs, ...) counts.
        busy = sorted(
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for c in resp["calendars"].values()
            for b in c.get("busy", [])
        )
        free = free_slots(
            s, e, busy, a.min_minutes, time.fromisoformat(a.day_start), time.fromisoformat(a.day_end)
        )
        return {
            "from": _fmt(s),
            "to": _fmt(e),
            "free": [
                {"start": _fmt(x), "end": _fmt(y), "minutes": int((y - x).total_seconds() // 60)}
                for x, y in free
            ],
            "busy": [{"start": _fmt(x), "end": _fmt(y)} for x, y in busy],
        }

    async def create_event(a: CreateArgs, ctx: ToolContext) -> Any:
        body = _event_body(
            a.title, a.start, a.end, location=a.location, description=a.description,
            attendees=[{"email": x} for x in a.attendees] if a.attendees else None,
        )  # fmt: skip
        details = {"Title": a.title, "When": _when_text(body), "Location": a.location or "",
                   "Invite": ", ".join(a.attendees or [])}  # fmt: skip
        target = (await cal.resolve(a.calendar_id))[0]
        details["Calendar"] = target["name"]
        if not await ctx.request_approval(ApprovalRequest("calendar.create_event", "Add Event", details)):
            return {"status": "not created: the user declined or didn't confirm"}
        ev = await cal.call(lambda svc: svc.events().insert(calendarId=target["id"], body=body))
        return {"status": "created", **normalize(ev, target["id"], target["name"])}

    async def update_event(a: UpdateArgs, ctx: ToolContext) -> Any:
        current = normalize(
            await cal.call(lambda svc: svc.events().get(calendarId=a.calendar_id, eventId=a.event_id)),
            a.calendar_id,
        )
        # Moving the start without an end keeps the original duration.
        end = a.end
        if a.start and not a.end and not current["all_day"] and "T" in a.start:
            duration = datetime.fromisoformat(current["end_iso"]) - datetime.fromisoformat(
                current["start_iso"]
            )
            end = (parse_when(a.start) + duration).isoformat()
        body = _event_body(a.title, a.start, end, location=a.location, description=a.description)
        if not body:
            raise ToolError("nothing to change")
        details = {"Event": current["title"], "Currently": f"{current['start']} – {current['end']}"}
        if a.title:
            details["New title"] = a.title
        if "start" in body:
            details["New time"] = _when_text(body)
        if a.location:
            details["New location"] = a.location
        if not await ctx.request_approval(ApprovalRequest("calendar.update_event", "Change Event", details)):
            return {"status": "not changed: the user declined or didn't confirm"}
        ev = await cal.call(
            lambda svc: svc.events().patch(calendarId=a.calendar_id, eventId=a.event_id, body=body)
        )
        return {"status": "updated", **normalize(ev, a.calendar_id)}

    async def delete_event(a: EventArgs, ctx: ToolContext) -> Any:
        current = normalize(
            await cal.call(lambda svc: svc.events().get(calendarId=a.calendar_id, eventId=a.event_id)),
            a.calendar_id,
        )
        details = {"Event": current["title"], "When": f"{current['start']} – {current['end']}"}
        if not await ctx.request_approval(ApprovalRequest("calendar.delete_event", "Delete Event", details)):
            return {"status": "not deleted: the user declined or didn't confirm"}
        await cal.call(lambda svc: svc.events().delete(calendarId=a.calendar_id, eventId=a.event_id))
        return {"status": "deleted", "title": current["title"]}

    async def open_event(a: EventArgs, ctx: ToolContext) -> Any:
        ev = await cal.call(lambda svc: svc.events().get(calendarId=a.calendar_id, eventId=a.event_id))
        if url := ev.get("htmlLink"):
            await ctx.open_url(url)
        return {"opened": bool(url)}

    R, A = Permission.READ, Permission.APPROVAL
    return [
        Tool("calendar.list_calendars", "List the user's calendars (classes, clubs, shared and subscribed ones).",
             NoArgs, list_calendars, R, label="Listed calendars"),
        Tool("calendar.get_events", "List events in a time range (default: today) across all the user's "
             "calendars, or one calendar by name. Each event says which calendar it's on.", RangeArgs,
             get_events, R, untrusted=True, label="Checked calendar"),
        Tool("calendar.get_event", "Get one event.", EventArgs, get_event, R, untrusted=True, label="Read an event"),
        Tool("calendar.search", "Search events by text across all the user's calendars.", SearchArgs, search, R,
             untrusted=True,
             label="Searched calendar"),
        Tool("calendar.find_free_time", "Find free slots between start and end.", FreeTimeArgs, find_free_time, R,
             label="Found free time"),
        Tool("calendar.create_event", "Create an event. The user is asked to approve first.", CreateArgs,
             create_event, A, timeout_s=180, label="Added event"),
        Tool("calendar.update_event", "Change an event (time, title, location). The user is asked to approve first.",
             UpdateArgs, update_event, A, timeout_s=180, label="Changed event"),
        Tool("calendar.delete_event", "Delete an event. The user is asked to approve first.", EventArgs,
             delete_event, A, timeout_s=180, label="Deleted event"),
        Tool("calendar.open_event", "Open an event in Google Calendar on screen.", EventArgs, open_event, R,
             label="Opened event"),
    ]  # fmt: skip


def free_slots(
    start: datetime,
    end: datetime,
    busy: list[tuple[datetime, datetime]],
    min_minutes: int,
    day_start: time,
    day_end: time,
) -> list[tuple[datetime, datetime]]:
    """Gaps of at least `min_minutes` between busy blocks, within waking hours."""
    tz = start.tzinfo
    slots: list[tuple[datetime, datetime]] = []
    day = start.date()
    while day <= end.date():
        lo = max(start, datetime.combine(day, day_start, tz))
        hi = min(end, datetime.combine(day, day_end, tz))
        cursor = lo
        for b_start, b_end in busy:
            if b_end <= cursor or b_start >= hi:
                continue
            if b_start > cursor:
                slots.append((cursor, min(b_start, hi)))
            cursor = max(cursor, b_end)
        if cursor < hi:
            slots.append((cursor, hi))
        day += timedelta(days=1)
    return [(a, b) for a, b in slots if (b - a) >= timedelta(minutes=min_minutes)]
