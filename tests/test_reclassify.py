"""Tests for claude_recall.reclassify (v0.8.4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_recall import content_kinds, reclassify, storage


def _seed_message(
    conn: sqlite3.Connection,
    *,
    msg_id: int,
    session_id: str,
    role: str,
    content: str,
    content_kind: str | None,
    project_slug: str = "proj-A",
    turn_index: int = 0,
):
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, project_slug, file_path, "
        "file_mtime, started_at, ended_at, turn_count, indexed_at) "
        "VALUES (?, ?, ?, 0, ?, ?, 0, ?)",
        (session_id, project_slug, f"/test/{session_id}.jsonl",
         "2026-04-30", "2026-04-30", "2026-04-30T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO messages(msg_id, session_id, role, content, turn_index, "
        "timestamp, content_hash, content_kind) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (msg_id, session_id, role, content, turn_index, "2026-04-30",
         "h", content_kind),
    )
    conn.commit()


def test_reclassify_updates_changed_rows(db_conn):
    """Seed messages with deliberately wrong content_kind. After reclassify,
    every row reflects the canonical classifier verdict."""
    # User message currently mis-labeled HARNESS — should become THOUGHT.
    _seed_message(
        db_conn, msg_id=1, session_id="s1", role="user",
        content="Should we add a rate limiter to the API?",
        content_kind="HARNESS",
    )
    # Assistant procedural message currently mis-labeled THOUGHT.
    _seed_message(
        db_conn, msg_id=2, session_id="s1", role="assistant",
        content="Let me check the existing patterns.",
        content_kind="THOUGHT", turn_index=1,
    )
    # Wrapper-tag harness message currently mis-labeled THOUGHT.
    _seed_message(
        db_conn, msg_id=3, session_id="s1", role="user",
        content="<ide_opened_file>foo.md</ide_opened_file>",
        content_kind="THOUGHT", turn_index=2,
    )

    report = reclassify.run_reclassify(db_conn)

    assert report.total_examined == 3
    assert report.rows_changed == 3
    assert report.dry_run is False

    rows = db_conn.execute(
        "SELECT msg_id, content_kind FROM messages ORDER BY msg_id"
    ).fetchall()
    kinds = {r["msg_id"]: r["content_kind"] for r in rows}
    assert kinds[1] == content_kinds.THOUGHT
    assert kinds[2] == content_kinds.PROCEDURAL
    assert kinds[3] == content_kinds.HARNESS


def test_reclassify_skips_already_correct_rows(db_conn):
    """Rows that already have the right classification aren't re-written.
    The report's rows_changed reflects only actual differences."""
    _seed_message(
        db_conn, msg_id=1, session_id="s1", role="user",
        content="Real substantive design discussion content.",
        content_kind="THOUGHT",
    )
    _seed_message(
        db_conn, msg_id=2, session_id="s1", role="assistant",
        content="Let me check.",
        content_kind="PROCEDURAL", turn_index=1,
    )

    report = reclassify.run_reclassify(db_conn)
    assert report.total_examined == 2
    assert report.rows_changed == 0


def test_reclassify_dry_run_does_not_write(db_conn):
    """--dry-run reports what would change but doesn't touch the DB."""
    _seed_message(
        db_conn, msg_id=1, session_id="s1", role="assistant",
        content="Let me check the build.",
        content_kind="THOUGHT",  # would change to PROCEDURAL
    )

    report = reclassify.run_reclassify(db_conn, dry_run=True)
    assert report.dry_run is True
    assert report.rows_changed == 1

    # DB unchanged.
    row = db_conn.execute(
        "SELECT content_kind FROM messages WHERE msg_id = 1"
    ).fetchone()
    assert row["content_kind"] == "THOUGHT"


def test_reclassify_scopes_to_project(db_conn):
    """`--project <slug>` only touches that project's rows."""
    _seed_message(
        db_conn, msg_id=1, session_id="s-a", role="assistant",
        content="Let me check.",
        content_kind="THOUGHT",  # would change in project A
        project_slug="proj-A",
    )
    _seed_message(
        db_conn, msg_id=2, session_id="s-b", role="assistant",
        content="Let me verify.",
        content_kind="THOUGHT",  # would change in project B
        project_slug="proj-B",
    )

    report = reclassify.run_reclassify(db_conn, project_slug="proj-A")
    assert report.total_examined == 1
    assert report.rows_changed == 1

    # proj-A msg got reclassified.
    a_kind = db_conn.execute(
        "SELECT content_kind FROM messages WHERE msg_id = 1"
    ).fetchone()["content_kind"]
    assert a_kind == content_kinds.PROCEDURAL

    # proj-B msg untouched.
    b_kind = db_conn.execute(
        "SELECT content_kind FROM messages WHERE msg_id = 2"
    ).fetchone()["content_kind"]
    assert b_kind == "THOUGHT"


def test_reclassify_handles_null_kind_rows(db_conn):
    """Rows with NULL content_kind (interrupted v3 migration) get
    classified by reclassify just like any other row. The dictionary-key
    ``None`` would crash format_report — verify the pre-distribution
    treats NULL as a usable bucket."""
    _seed_message(
        db_conn, msg_id=1, session_id="s1", role="user",
        content="Real thought content.",
        content_kind=None,
    )

    report = reclassify.run_reclassify(db_conn)
    assert report.total_examined == 1
    assert report.rows_changed == 1
    row = db_conn.execute(
        "SELECT content_kind FROM messages WHERE msg_id = 1"
    ).fetchone()
    assert row["content_kind"] == content_kinds.THOUGHT


def test_reclassify_format_report_shows_deltas(db_conn):
    """The CLI text output is the report's format_report() — verify it
    includes pre/post counts and the changed-rows total."""
    _seed_message(
        db_conn, msg_id=1, session_id="s1", role="user",
        content="<ide_opened_file>x.md</ide_opened_file>",
        content_kind="THOUGHT",
    )
    _seed_message(
        db_conn, msg_id=2, session_id="s1", role="assistant",
        content="Let me check.",
        content_kind="THOUGHT", turn_index=1,
    )

    report = reclassify.run_reclassify(db_conn)
    text = reclassify.format_report(report)

    assert "Reclassified 2 messages" in text
    assert "HARNESS" in text
    assert "PROCEDURAL" in text
    assert "THOUGHT" in text
    # Both deltas should appear: HARNESS +1, PROCEDURAL +1, THOUGHT -2.
    assert "+1" in text
    assert "-2" in text
    assert "2 rows updated" in text


def test_reclassify_dry_run_format_report_says_no_writes(db_conn):
    _seed_message(
        db_conn, msg_id=1, session_id="s1", role="assistant",
        content="Let me run the tests.",
        content_kind="THOUGHT",
    )
    report = reclassify.run_reclassify(db_conn, dry_run=True)
    text = reclassify.format_report(report)
    assert "[dry-run]" in text
    assert "No writes" in text


def test_reclassify_empty_archive_returns_zero(db_conn):
    """Empty messages table doesn't blow up — returns a zeroed report."""
    report = reclassify.run_reclassify(db_conn)
    assert report.total_examined == 0
    assert report.rows_changed == 0
    text = reclassify.format_report(report)
    assert "0 messages" in text
