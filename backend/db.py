"""SQLite database (spec §47): conversations, their messages, and memories.

Schema changes are appended to MIGRATIONS, never edited in place; the
database's user_version records how many have run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS = [
    """
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX conversations_updated ON conversations(updated_at);
    CREATE TABLE messages (
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        role TEXT NOT NULL,
        body TEXT NOT NULL,  -- the whole Message as JSON, tool calls included
        created_at REAL NOT NULL,
        PRIMARY KEY (conversation_id, seq)
    );
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the database. Autocommit mode:
    every write is a single statement, so there is nothing to batch."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN; {script}; PRAGMA user_version={i}; COMMIT;")
    return conn
