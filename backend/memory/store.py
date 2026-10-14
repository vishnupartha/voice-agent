"""Facts the user asked the assistant to remember."""

from __future__ import annotations

import sqlite3
import time


class MemoryStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def all(self) -> list[tuple[int, str]]:
        return [(r["id"], r["text"]) for r in self.db.execute("SELECT id, text FROM memories ORDER BY id")]

    def add(self, text: str) -> int:
        cur = self.db.execute("INSERT INTO memories (text, created_at) VALUES (?, ?)", (text, time.time()))
        return int(cur.lastrowid or 0)

    def remove(self, memory_id: int) -> str | None:
        """Delete one memory; its text, or None if there was no such memory."""
        row = self.db.execute("DELETE FROM memories WHERE id = ? RETURNING text", (memory_id,)).fetchone()
        return row["text"] if row else None
