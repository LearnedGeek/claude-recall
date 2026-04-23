"""Tests for claude_recall.storage.

See docs/PLAN.md section 11 for the full test plan.
"""

import sqlite3
from datetime import UTC, datetime

import pytest

from claude_recall import storage


def test_open_db_creates_schema(temp_db):
    """open_db() creates the schema and returns a working connection."""
    assert not temp_db.exists()
    conn = storage.open_db(temp_db)
    try:
        assert temp_db.exists()
        assert isinstance(conn, sqlite3.Connection)
        assert conn.row_factory is sqlite3.Row

        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"sessions", "messages", "schema_version"}.issubset(tables)

        # Virtual FTS5 table exists
        fts_row = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone()
        assert fts_row is not None
    finally:
        conn.close()


def test_schema_version_is_current(db_conn):
    """Schema version is recorded and matches SCHEMA_VERSION constant."""
    row = db_conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == storage.SCHEMA_VERSION


def test_fts5_available(db_conn):
    """FTS5 is available in this SQLite build."""
    assert storage.fts5_available(db_conn) is True


def test_messages_fts_sync_on_insert(db_conn):
    """Inserting a message also populates messages_fts via trigger."""
    now = datetime.now(UTC).isoformat()
    db_conn.execute(
        "INSERT INTO sessions(session_id, project_slug, file_path, file_mtime, "
        "turn_count, indexed_at) VALUES (?,?,?,?,?,?)",
        ("s1", "demo", "/tmp/demo.jsonl", 1.0, 1, now),
    )
    db_conn.execute(
        "INSERT INTO messages(session_id, role, content, turn_index, timestamp) "
        "VALUES (?,?,?,?,?)",
        ("s1", "user", "Regex patterns are fragile for classification.", 0, now),
    )
    db_conn.commit()

    row = db_conn.execute(
        "SELECT rowid, content FROM messages_fts WHERE messages_fts MATCH ?",
        ("regex",),
    ).fetchone()
    assert row is not None
    assert "regex" in row["content"].lower()


def test_reopen_existing_db_idempotent(temp_db):
    """Re-opening an existing DB does not duplicate schema_version rows."""
    conn1 = storage.open_db(temp_db)
    conn1.close()
    conn2 = storage.open_db(temp_db)
    try:
        count = conn2.execute(
            "SELECT COUNT(*) AS c FROM schema_version WHERE version=?",
            (storage.SCHEMA_VERSION,),
        ).fetchone()["c"]
        assert count == 1
    finally:
        conn2.close()


def test_open_db_raises_when_fts5_unavailable(temp_db, monkeypatch):
    """If FTS5 probe fails, open_db raises StorageError and does not create schema."""
    monkeypatch.setattr(storage, "fts5_available", lambda conn: False)
    with pytest.raises(storage.StorageError):
        storage.open_db(temp_db)


def test_fts5_probe_returns_false_on_error():
    """fts5_available returns False when the connection rejects the FTS5 DDL."""

    class FakeConn:
        def execute(self, sql):
            raise sqlite3.OperationalError("no such module: fts5")

    assert storage.fts5_available(FakeConn()) is False


def test_message_vectors_table_exists(db_conn):
    """message_vectors is created unconditionally for v0.3 embeddings layer."""
    row = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='message_vectors'"
    ).fetchone()
    assert row is not None


def test_message_vectors_cascade_on_session_delete(db_conn):
    """Deleting a session removes its vectors via message → session ON DELETE CASCADE."""
    now = datetime.now(UTC).isoformat()
    db_conn.execute(
        "INSERT INTO sessions(session_id, project_slug, file_path, file_mtime, "
        "turn_count, indexed_at) VALUES (?,?,?,?,?,?)",
        ("s1", "demo", "/tmp/demo.jsonl", 1.0, 0, now),
    )
    cur = db_conn.execute(
        "INSERT INTO messages(session_id, role, content, turn_index) VALUES (?,?,?,?)",
        ("s1", "user", "hello", 0),
    )
    msg_id = cur.lastrowid
    db_conn.execute(
        "INSERT INTO message_vectors(msg_id, vector, model, dim, embedded_at) "
        "VALUES (?,?,?,?,?)",
        (msg_id, b"\x00" * 12, "nomic-embed-text", 3, now),
    )
    db_conn.commit()

    db_conn.execute("DELETE FROM sessions WHERE session_id=?", ("s1",))
    db_conn.commit()

    remaining = db_conn.execute(
        "SELECT COUNT(*) AS c FROM message_vectors WHERE msg_id=?", (msg_id,)
    ).fetchone()["c"]
    assert remaining == 0


def test_cascade_delete_removes_messages(db_conn):
    """Deleting a session removes its messages via ON DELETE CASCADE."""
    now = datetime.now(UTC).isoformat()
    db_conn.execute(
        "INSERT INTO sessions(session_id, project_slug, file_path, file_mtime, "
        "turn_count, indexed_at) VALUES (?,?,?,?,?,?)",
        ("s1", "demo", "/tmp/demo.jsonl", 1.0, 0, now),
    )
    db_conn.execute(
        "INSERT INTO messages(session_id, role, content, turn_index) VALUES (?,?,?,?)",
        ("s1", "user", "hello", 0),
    )
    db_conn.commit()

    db_conn.execute("DELETE FROM sessions WHERE session_id=?", ("s1",))
    db_conn.commit()

    remaining = db_conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id=?", ("s1",)
    ).fetchone()["c"]
    assert remaining == 0
