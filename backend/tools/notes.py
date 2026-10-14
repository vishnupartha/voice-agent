"""Apple Notes tools, through JavaScript for Automation (`osascript`): Notes has
no other API. The backend runs as the app's child process, so macOS asks once
whether Personal Agent may control Notes.

The assistant has its own folder, AGENT_FOLDER, where it creates, reads and
appends freely. Notes anywhere else are the user's: searching, reading or
adding to them asks for approval every time (enforced here, not left to the
model). Moving a note into the folder shares it for good.
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime
from time import monotonic
from typing import Any

import orjson
from pydantic import BaseModel, Field

from backend.agent.tools import ApprovalRequest, Permission, Tool, ToolContext, ToolError

AGENT_FOLDER = "Personal Agent"
NOTE_CHARS = 8_000  # per note read, so one long note can't flood the context
SNIPPET_CHARS = 160
# Notes found by a search the user approved can be read without asking again for a while.
GRANT_S = 10 * 60

# Each script gets its arguments as one JSON string (never spliced into the
# code) and prints its result as JSON.
_PRELUDE = """
const Notes = Application('Notes');
const DELETED = 'Recently Deleted';
const AGENT_FOLDER = __AGENT_FOLDER__;
function agentFolder() {
  const account = Notes.defaultAccount();
  const found = account.folders.whose({name: AGENT_FOLDER})();
  if (found.length) return found[0];
  account.folders.push(Notes.Folder({name: AGENT_FOLDER}));
  return account.folders.whose({name: AGENT_FOLDER})()[0];
}
// The user's own notes need their approval first: the caller retries with
// anywhere=true once they've said yes.
function checkShared(n, anywhere) {
  if (!anywhere && n.container().name() !== AGENT_FOLDER) throw new Error('NOT_SHARED:' + n.name());
}
function describe(n, chars) {
  const text = n.passwordProtected() ? '' : n.plaintext();
  const firstBreak = text.indexOf('\\n');
  const rest = firstBreak < 0 ? '' : text.slice(firstBreak + 1).trim();
  return {
    id: n.id(),
    title: n.name(),
    folder: n.container().name(),
    modified: n.modificationDate().toISOString(),
    locked: n.passwordProtected(),
    text: rest.slice(0, chars),
    truncated: rest.length > chars,
  };
}
function noteById(id) {
  try { const n = Notes.notes.byId(id); n.name(); return n; }
  catch (e) { throw new Error('no note with that id; search again'); }
}
""".replace("__AGENT_FOLDER__", orjson.dumps(AGENT_FOLDER).decode())

_SCRIPTS = {
    "search": """
function run(argv) {
  const a = JSON.parse(argv[0]);
  const q = a.query;
  const scope = a.everywhere ? Notes.notes : agentFolder().notes;
  const matches = q ? scope.whose({_or: [{name: {_contains: q}}, {plaintext: {_contains: q}}]})() : scope();
  const out = [];
  for (const n of matches) {
    if (n.container().name() === DELETED) continue;
    out.push(describe(n, a.snippet));
  }
  out.sort((x, y) => y.modified.localeCompare(x.modified));
  return JSON.stringify(out.slice(0, a.limit));
}
""",
    "read": """
function run(argv) {
  const a = JSON.parse(argv[0]);
  const n = noteById(a.id);
  checkShared(n, a.anywhere);
  if (n.passwordProtected()) throw new Error('that note is locked');
  return JSON.stringify(describe(n, a.chars));
}
""",
    "create": """
function run(argv) {
  const a = JSON.parse(argv[0]);
  const n = Notes.Note({body: a.html});
  agentFolder().notes.push(n);
  return JSON.stringify(describe(noteById(n.id()), a.snippet));
}
""",
    "append": """
function run(argv) {
  const a = JSON.parse(argv[0]);
  const n = noteById(a.id);
  checkShared(n, a.anywhere);
  if (n.passwordProtected()) throw new Error('that note is locked');
  n.body = n.body() + a.html;
  return JSON.stringify(describe(noteById(a.id), a.snippet));
}
""",
}


def to_html(text: str) -> str:
    """Plain text as Notes body HTML: one <div> per line."""
    return "".join(
        f"<div>{html.escape(line)}</div>" if line.strip() else "<div><br></div>" for line in text.split("\n")
    )


async def run_script(name: str, **args: Any) -> Any:
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript",
        "-l",
        "JavaScript",
        "-e",
        _PRELUDE + _SCRIPTS[name],
        orjson.dumps(args).decode(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:  # timed out or the turn was cancelled
        proc.kill()
        raise
    if proc.returncode != 0:
        stderr = err.decode(errors="replace")
        if "NOT_SHARED:" in stderr:
            raise NotShared(stderr.split("NOT_SHARED:", 1)[1].rsplit(" (", 1)[0].strip())
        raise ToolError(_explain(stderr))
    return orjson.loads(out)


class NotShared(Exception):
    """The note is the user's own (outside AGENT_FOLDER); carries its title."""


def _explain(stderr: str) -> str:
    if "-1743" in stderr or "Not authorized" in stderr:
        return (
            "Personal Agent isn't allowed to use Notes. Turn it on in System Settings → "
            "Privacy & Security → Automation → Personal Agent → Notes."
        )
    # osascript errors look like "execution error: Error: no note with that id (-2700)"
    message = stderr.strip().split("Error: ")[-1]
    return message.rsplit(" (", 1)[0] or "Notes couldn't do that"


def _readable(note: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in note.items() if v not in (None, "", False)}
    out["modified"] = datetime.fromisoformat(note["modified"]).astimezone().strftime("%a %b %-d, %Y")
    return out


class SearchArgs(BaseModel):
    query: str = Field("", description="Words in the title or text; empty: the most recent notes")
    everywhere: bool = Field(
        False, description="Also search the user's own notes, not just yours; they're asked to approve"
    )
    limit: int = Field(8, ge=1, le=20)


class IdArgs(BaseModel):
    id: str


class CreateArgs(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field("", max_length=20_000, description="Plain text; new lines start new lines")


class AppendArgs(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=20_000)


DECLINED = {"status": "not allowed: the user declined or didn't confirm"}


def notes_tools() -> list[Tool]:
    granted: dict[str, float] = {}  # note id -> when read access to it expires

    def readable_now(note_id: str) -> bool:
        return granted.get(note_id, 0) > monotonic()

    async def search(a: SearchArgs, ctx: ToolContext) -> Any:
        if a.everywhere:
            details = {"Looking for": a.query or "your most recent notes"}
            if not await ctx.request_approval(ApprovalRequest("notes.search", "Search Your Notes", details)):
                return DECLINED
        found = await run_script(
            "search", query=a.query, everywhere=a.everywhere, limit=a.limit, snippet=SNIPPET_CHARS
        )
        for n in found:
            granted[n["id"]] = monotonic() + GRANT_S
        return [_readable(n) for n in found]

    async def read(a: IdArgs, ctx: ToolContext) -> Any:
        try:
            note = await run_script("read", id=a.id, anywhere=readable_now(a.id), chars=NOTE_CHARS)
        except NotShared as e:
            if not await ctx.request_approval(
                ApprovalRequest("notes.read", "Read Your Note", {"Note": str(e)})
            ):
                return DECLINED
            note = await run_script("read", id=a.id, anywhere=True, chars=NOTE_CHARS)
        return _readable(note)

    async def create(a: CreateArgs, ctx: ToolContext) -> Any:
        body = to_html(a.title) + (to_html(a.body) if a.body else "")
        note = await run_script("create", html=body, snippet=SNIPPET_CHARS)
        return {"status": "created", **_readable(note)}

    async def append(a: AppendArgs, ctx: ToolContext) -> Any:
        # Changing the user's own note always asks, even if they let it read the note.
        try:
            note = await run_script(
                "append", id=a.id, anywhere=False, html=to_html(a.text), snippet=SNIPPET_CHARS
            )
        except NotShared as e:
            details = {"Note": str(e), "Adding": a.text}
            if not await ctx.request_approval(ApprovalRequest("notes.append", "Add to Your Note", details)):
                return DECLINED
            note = await run_script(
                "append", id=a.id, anywhere=True, html=to_html(a.text), snippet=SNIPPET_CHARS
            )
        return {"status": "appended", "id": note["id"], "title": note["title"]}

    # Generous timeouts: the first call launches Notes and may wait on the
    # permission prompt, and the user's notes wait on an approval.
    R, W = Permission.READ, Permission.WRITE
    return [
        Tool("notes.search", f"Search Apple Notes by words in the title or text, newest first. By default "
             f"only your own notes (the \"{AGENT_FOLDER}\" folder); everywhere=true includes the user's notes "
             "and asks them first. Gives each note's id, title, folder and the start of its text.",
             SearchArgs, search, R, timeout_s=180, untrusted=True, label="Searched notes"),
        Tool("notes.read", "Read one note in full, by its id from notes.search. The user's own notes may "
             "ask them first.", IdArgs, read, R, timeout_s=180, untrusted=True, label="Read a note"),
        Tool("notes.create", f"Create an Apple Note in your \"{AGENT_FOLDER}\" folder. Runs without asking; "
             "tell the user what you saved.", CreateArgs, create, W, timeout_s=70, label="Created note"),
        Tool("notes.append", "Add text to the end of a note, by its id from notes.search (\"add milk to the "
             "grocery list\"). Adding to the user's own notes asks them first.", AppendArgs, append, W,
             timeout_s=180, label="Added to note"),
    ]  # fmt: skip
