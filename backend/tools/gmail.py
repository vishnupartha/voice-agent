"""Gmail tools. Reads are live API calls; the local cache and
history sync come later. Sending always goes through approval."""

from __future__ import annotations

import asyncio
import base64
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from backend.agent.tools import ApprovalRequest, Permission, Tool, ToolContext, ToolError
from backend.integrations.google_auth import GoogleAuth, GoogleNotConnected

BODY_CHARS = 4_000  # per message, so one long email can't flood the context


class Gmail:
    def __init__(self, auth: GoogleAuth) -> None:
        self.auth = auth

    async def _call(self, fn: Any) -> Any:
        """Run a googleapiclient request builder in a thread with fresh credentials."""
        try:
            creds = await self.auth.credentials()
        except GoogleNotConnected as e:
            raise ToolError(str(e)) from e

        def run() -> Any:
            from googleapiclient.discovery import build

            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            return fn(service.users()).execute()

        return await asyncio.to_thread(run)

    # ---- helpers ----

    async def _list(self, query: str, max_results: int) -> list[dict[str, Any]]:
        resp = await self._call(lambda u: u.messages().list(userId="me", q=query, maxResults=max_results))
        ids = [m["id"] for m in resp.get("messages", [])]
        metas = await asyncio.gather(
            *(
                self._call(
                    lambda u, i=i: u.messages().get(
                        userId="me",
                        id=i,
                        format="metadata",
                        metadataHeaders=["From", "To", "Subject", "Date"],
                    )
                )
                for i in ids
            )
        )
        return [summarize(m) for m in metas]

    async def _thread(self, thread_id: str) -> dict[str, Any]:
        t = await self._call(lambda u: u.threads().get(userId="me", id=thread_id, format="full"))
        return t

    async def _draft(self, draft_id: str) -> dict[str, Any]:
        return await self._call(lambda u: u.drafts().get(userId="me", id=draft_id, format="full"))


# ---- message parsing ----


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).astimezone().strftime("%a %b %-d, %-I:%M %p")
    except (TypeError, ValueError):
        return raw


def summarize(msg: dict[str, Any]) -> dict[str, Any]:
    h = _headers(msg)
    labels = msg.get("labelIds", [])
    return {
        "message_id": msg["id"],
        "thread_id": msg.get("threadId"),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "subject": h.get("subject", "(no subject)"),
        "date": _date(h.get("date", "")),
        "snippet": msg.get("snippet", ""),
        "unread": "UNREAD" in labels,
        "important": "IMPORTANT" in labels,
    }


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def body_text(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to text extracted from HTML."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for p in part.get("parts", []):
            walk(p)

    walk(payload)
    if plain:
        text = "\n".join(plain)
    elif html:
        text = BeautifulSoup("\n".join(html), "html.parser").get_text("\n")
    else:
        text = ""
    text = _strip_quoted(text)
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()
    return text[:BODY_CHARS] + (" …[truncated]" if len(text) > BODY_CHARS else "")


def _strip_quoted(text: str) -> str:
    """Drop the quoted history under replies ("On Mon, X wrote:" / "> ...")."""
    lines = []
    for line in text.splitlines():
        if re.match(r"^On .+ wrote:\s*$", line.strip()) or line.startswith("-----Original Message"):
            break
        if not line.startswith(">"):
            lines.append(line)
    return "\n".join(lines)


def full_message(msg: dict[str, Any]) -> dict[str, Any]:
    return {**summarize(msg), "body": body_text(msg.get("payload", {}))}


def _raw(
    to: str, subject: str, body: str, cc: str | None, bcc: str | None, reply_to: dict[str, str] | None
) -> str:
    m = EmailMessage()
    m["To"] = to
    if cc:
        m["Cc"] = cc
    if bcc:
        m["Bcc"] = bcc
    m["Subject"] = subject
    if reply_to:
        m["In-Reply-To"] = reply_to["message-id"]
        m["References"] = (reply_to.get("references", "") + " " + reply_to["message-id"]).strip()
    m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


def _draft_view(draft: dict[str, Any]) -> dict[str, Any]:
    msg = draft.get("message", {})
    h = _headers(msg)
    return {
        "draft_id": draft["id"],
        "thread_id": msg.get("threadId"),
        "to": h.get("to", ""),
        "cc": h.get("cc", ""),
        "subject": h.get("subject", ""),
        "body": body_text(msg.get("payload", {})),
    }


# ---- tool arguments ----


class SearchArgs(BaseModel):
    query: str = Field(
        description="Gmail search syntax, e.g. 'from:alice newer_than:7d' or 'subject:interview'"
    )
    max_results: int = Field(10, ge=1, le=25)


class MessageArgs(BaseModel):
    message_id: str


class ThreadArgs(BaseModel):
    thread_id: str


class RecentArgs(BaseModel):
    hours: int = Field(24, ge=1, le=24 * 30)
    max_results: int = Field(15, ge=1, le=25)


class MaxArgs(BaseModel):
    max_results: int = Field(10, ge=1, le=25)


class CreateDraftArgs(BaseModel):
    to: str = Field(description="Recipient address(es), comma-separated")
    subject: str
    body: str = Field(description="Plain-text body, signed as the user")
    thread_id: str | None = Field(None, description="Set to reply within an existing thread")
    cc: str | None = None
    bcc: str | None = None


class UpdateDraftArgs(BaseModel):
    draft_id: str
    body: str | None = Field(None, description="The complete new body (not a diff)")
    subject: str | None = None
    to: str | None = None
    cc: str | None = None
    bcc: str | None = None


class DraftArgs(BaseModel):
    draft_id: str


# ---- tools ----


def gmail_tools(gmail: Gmail) -> list[Tool]:
    async def search(a: SearchArgs, ctx: ToolContext) -> Any:
        results = await gmail._list(a.query, a.max_results)
        return {"query": a.query, "count": len(results), "messages": results}

    async def get_message(a: MessageArgs, ctx: ToolContext) -> Any:
        return full_message(
            await gmail._call(lambda u: u.messages().get(userId="me", id=a.message_id, format="full"))
        )

    async def get_thread(a: ThreadArgs, ctx: ToolContext) -> Any:
        t = await gmail._thread(a.thread_id)
        msgs = t.get("messages", [])
        # Most recent messages matter most; keep the last six in full.
        return {
            "thread_id": a.thread_id,
            "message_count": len(msgs),
            "messages": [full_message(m) for m in msgs[-6:]],
        }

    async def get_recent(a: RecentArgs, ctx: ToolContext) -> Any:
        after = int((datetime.now() - timedelta(hours=a.hours)).timestamp())
        results = await gmail._list(f"in:inbox after:{after}", a.max_results)
        return {"hours": a.hours, "count": len(results), "messages": results}

    async def get_unread(a: MaxArgs, ctx: ToolContext) -> Any:
        results = await gmail._list("in:inbox is:unread", a.max_results)
        return {"count": len(results), "messages": results}

    async def get_important(a: MaxArgs, ctx: ToolContext) -> Any:
        results = await gmail._list("in:inbox is:important newer_than:7d -category:promotions", a.max_results)
        return {"count": len(results), "messages": results}

    async def open_in_browser(a: ThreadArgs, ctx: ToolContext) -> Any:
        await ctx.open_url(f"https://mail.google.com/mail/u/0/#all/{a.thread_id}")
        return {"opened": True}

    async def create_draft(a: CreateDraftArgs, ctx: ToolContext) -> Any:
        reply_to = None
        subject = a.subject
        if a.thread_id:
            last = (await gmail._thread(a.thread_id)).get("messages", [])[-1]
            h = _headers(last)
            if "message-id" in h:
                reply_to = {"message-id": h["message-id"], "references": h.get("references", "")}
            if not subject.lower().startswith("re:"):
                subject = "Re: " + (h.get("subject") or subject)
        message: dict[str, Any] = {"raw": _raw(a.to, subject, a.body, a.cc, a.bcc, reply_to)}
        if a.thread_id:
            message["threadId"] = a.thread_id
        draft = await gmail._call(lambda u: u.drafts().create(userId="me", body={"message": message}))
        view = _draft_view(await gmail._draft(draft["id"]))
        await ctx.emit("draft.updated", draft=view)
        return {"status": "draft saved (not sent)", **view}

    async def update_draft(a: UpdateDraftArgs, ctx: ToolContext) -> Any:
        current = await gmail._draft(a.draft_id)
        msg = current.get("message", {})
        h = _headers(msg)
        thread_id = msg.get("threadId")
        reply_to = (
            {"message-id": h["in-reply-to"], "references": h.get("references", "")}
            if "in-reply-to" in h
            else None
        )
        raw = _raw(
            a.to or h.get("to", ""),
            a.subject or h.get("subject", ""),
            a.body if a.body is not None else body_text(msg.get("payload", {})),
            a.cc if a.cc is not None else h.get("cc"),
            a.bcc if a.bcc is not None else h.get("bcc"),
            reply_to,
        )
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        await gmail._call(lambda u: u.drafts().update(userId="me", id=a.draft_id, body={"message": message}))
        view = _draft_view(await gmail._draft(a.draft_id))
        await ctx.emit("draft.updated", draft=view)
        return {"status": "draft updated (not sent)", **view}

    async def get_draft(a: DraftArgs, ctx: ToolContext) -> Any:
        return _draft_view(await gmail._draft(a.draft_id))

    async def send_draft(a: DraftArgs, ctx: ToolContext) -> Any:
        view = _draft_view(await gmail._draft(a.draft_id))
        recipients = ", ".join(addr or name for name, addr in getaddresses([view["to"]]))
        approved = await ctx.request_approval(
            ApprovalRequest(
                tool="gmail.send_draft",
                title="Send Email",
                details={
                    "To": view["to"],
                    "Cc": view["cc"],
                    "Subject": view["subject"],
                    "Body": view["body"],
                },
            )
        )
        if not approved:
            return {"status": "not sent: the user declined or didn't confirm", "draft_id": a.draft_id}
        sent = await gmail._call(lambda u: u.drafts().send(userId="me", body={"id": a.draft_id}))
        return {
            "status": "sent",
            "to": recipients,
            "message_id": sent.get("id"),
            "thread_id": sent.get("threadId"),
        }

    R, W, A = Permission.READ, Permission.WRITE, Permission.APPROVAL
    return [
        Tool("gmail.search", "Search the user's Gmail.", SearchArgs, search, R, untrusted=True, label="Searched Gmail"),
        Tool("gmail.get_message", "Read one email in full.", MessageArgs, get_message, R, untrusted=True,
             label="Read an email"),
        Tool("gmail.get_thread", "Read an email thread (latest messages in full).", ThreadArgs, get_thread, R,
             untrusted=True, label="Read a thread"),
        Tool("gmail.summarize_thread", "Fetch a thread so you can summarize it for the user.", ThreadArgs,
             get_thread, R, untrusted=True, label="Read a thread"),
        Tool("gmail.get_recent", "Inbox emails from the last N hours.", RecentArgs, get_recent, R, untrusted=True,
             label="Checked recent email"),
        Tool("gmail.get_unread", "Unread inbox emails.", MaxArgs, get_unread, R, untrusted=True,
             label="Checked unread email"),
        Tool("gmail.get_important", "Recent emails Gmail marks important (excluding promotions).", MaxArgs,
             get_important, R, untrusted=True, label="Checked important email"),
        Tool("gmail.open_in_browser", "Open a thread in Gmail on the Mac's screen.", ThreadArgs, open_in_browser, R,
             label="Opened in Gmail"),
        Tool("gmail.create_draft", "Save a new draft (or a reply when thread_id is set). Does not send.",
             CreateDraftArgs, create_draft, W, label="Drafted an email"),
        Tool("gmail.update_draft", "Change an existing draft. Pass the complete new body.", UpdateDraftArgs,
             update_draft, W, label="Updated the draft"),
        Tool("gmail.get_draft", "Read a draft.", DraftArgs, get_draft, R, label="Read the draft"),
        Tool("gmail.send_draft", "Send a draft. The user is asked to approve first.", DraftArgs, send_draft, A,
             timeout_s=180, label="Sent email"),
    ]  # fmt: skip
