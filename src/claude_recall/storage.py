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

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 2


def content_hash(content: str) -> str:
    """Stable per-message content fingerprint.

    Used by the v0.6 indexer to detect which messages in a session have
    actually changed across re-ingest, so the FK CASCADE on `message_vectors`
    only fires for messages whose content moved (issue #16). blake2b is
    stdlib + fast (~1µs/message); 16-byte digest is overkill-strong against
    accidental collisions across realistic corpus sizes.
    """
    return hashlib.blake2b(content.encode("utf-8"), digest_size=16).hexdigest()


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
    content_hash    TEXT,
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

-- v0.3 embeddings layer. Added unconditionally; unused when [embeddings].enabled=false.
-- See docs/EMBEDDINGS-PLAN.md §5 for the full rationale.
CREATE TABLE IF NOT EXISTS message_vectors (
    msg_id       INTEGER PRIMARY KEY,
    vector       BLOB    NOT NULL,
    model        TEXT    NOT NULL,
    dim          INTEGER NOT NULL,
    embedded_at  TEXT    NOT NULL,
    FOREIGN KEY (msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_message_vectors_model
    ON message_vectors(model, dim);
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
    # WAL mode lets concurrent readers (e.g., a status query) proceed while
    # an indexer writer holds the BEGIN IMMEDIATE lock. Required for v0.6's
    # transaction-safe per-file diff (a SessionStart hook firing during a
    # manual `index` run otherwise blocks both processes). PRAGMA persists
    # in the database file itself; safe to set on every open.
    conn.execute("PRAGMA journal_mode = WAL")

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
    """Install DDL, run any pending migrations, record current schema version.

    Order matters:
    1. SCHEMA_DDL is idempotent (CREATE IF NOT EXISTS) — adds tables for
       fresh installs but cannot add columns to existing tables. That's
       what the migration dispatcher below is for.
    2. Migrations gated on the current `schema_version` value run AFTER
       DDL so a fresh install (which sees v0 — schema_version table empty
       after DDL but before version-stamping) skips the migration entirely
       (the new column is already in SCHEMA_DDL).
    3. Version-stamp last, so a partial migration leaves the version
       behind and the next open retries.
    """
    conn.executescript(SCHEMA_DDL)

    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] if row and row["v"] is not None else 0

    if current < 2:
        _migrate_v1_to_v2(conn)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add `messages.content_hash` and backfill from existing content.

    Issue #16 (v0.6): the indexer needs a per-message change detector so
    routine re-ingest stops cascade-deleting unchanged vectors. This
    migration is non-destructive — the backfill computes hashes from the
    same `content` column the parser already ingested, so existing
    (msg_id, vector) pairs stay valid and no re-embed is required.

    The ALTER TABLE has no `IF NOT EXISTS` form in SQLite, so we
    introspect via `PRAGMA table_info` and skip if the column already
    exists. That makes the migration safe to re-run if a partial commit
    left the schema in a half-state.
    """
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN content_hash TEXT")

    rows = conn.execute(
        "SELECT msg_id, content FROM messages WHERE content_hash IS NULL"
    ).fetchall()
    if rows:
        conn.executemany(
            "UPDATE messages SET content_hash = ? WHERE msg_id = ?",
            [(content_hash(r["content"]), r["msg_id"]) for r in rows],
        )
