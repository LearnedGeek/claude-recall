"""SQLite connection management, schema initialization, and migrations.

Owns:
- Connection lifecycle (open, close, context manager)
- Schema DDL (see docs/PLAN.md section 5 for full schema)
- FTS5 availability check
- Schema version tracking + migration primitive
- File permission hardening (0600 on Unix)

Does NOT own:
- JSONL parsing (that's indexer.py)
- Query logic (that's search.py)
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    project_slug     TEXT NOT NULL,
    file_path        TEXT NOT NULL UNIQUE,
    file_mtime       REAL NOT NULL,
    started_at       TEXT,
    ended_at         TEXT,
    turn_count       INTEGER NOT NULL DEFAULT 0,
    indexed_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_project_started
    ON sessions(project_slug, started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    msg_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    turn_index      INTEGER NOT NULL,
    timestamp       TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_turn
    ON messages(session_id, turn_index);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp
    ON messages(timestamp DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='msg_id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.msg_id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.msg_id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.msg_id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.msg_id, new.content);
END;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


class StorageError(RuntimeError):
    """Raised when the database cannot be opened or initialized."""


def open_db(path: Path) -> sqlite3.Connection:
    """Open a connection, ensure schema is current, return connection.

    Creates parent directories as needed. Enables foreign keys, installs the
    schema, records the schema version, and on Unix hardens the file mode to
    0600.
    """
    path = Path(path)
    is_new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    if not fts5_available(conn):
        conn.close()
        raise StorageError(
            "FTS5 is not available in this SQLite build. "
            "Upgrade SQLite or install a Python build with FTS5 support."
        )

    _install_schema(conn)

    if is_new and sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    return conn


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Check whether this SQLite build supports FTS5.

    Attempts to create an FTS5 virtual table in a temporary scope and rolls it
    back. Returns True on success.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)"
        )
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _install_schema(conn: sqlite3.Connection) -> None:
    """Install DDL and record current schema version.

    This is the MVP migration primitive. Future versions add conditional
    ALTER TABLE steps here, gated on the current schema_version value.
    """
    conn.executescript(SCHEMA_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
