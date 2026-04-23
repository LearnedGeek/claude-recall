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

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# DDL — see docs/PLAN.md section 5.1 for annotations.
# TODO(implementer): move this to a .sql file and read at runtime, or keep inline.
# Decision deferred; both are acceptable.
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
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.msg_id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.msg_id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.msg_id, new.content);
END;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    """Open a connection, ensure schema is current, return connection.

    TODO(implementer):
    - Create parent directory if missing
    - Check FTS5 availability via sqlite_compile_options() or probe query
    - Execute SCHEMA_DDL
    - Run migrations based on schema_version
    - Set 0600 permissions on the db file (Unix)
    - Enable foreign keys PRAGMA
    - Return a Connection with row_factory = sqlite3.Row
    """
    raise NotImplementedError("See docs/PLAN.md section 10 for implementation order.")


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Check whether this SQLite build supports FTS5.

    TODO(implementer): attempt to CREATE VIRTUAL TABLE ... USING fts5 in a savepoint
    and roll back; or query sqlite_compile_options. Return bool.
    """
    raise NotImplementedError
