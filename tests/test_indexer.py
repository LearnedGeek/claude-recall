"""Tests for claude_recall.indexer.

See docs/PLAN.md section 11 for the full test plan.
"""

import os
import time

import pytest

from claude_recall import indexer


def _session_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]


def _message_count(conn, session_id: str | None = None) -> int:
    if session_id:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=?", (session_id,)
        ).fetchone()["c"]
    return conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]


def test_index_fresh_archive(archive_dir, db_conn):
    """First-time index of a clean archive inserts the expected sessions and messages."""
    report = indexer.run_index(db_conn, archive_dir)
    # Three fixture files in the test-project dir
    assert report.new_sessions == 3
    assert report.updated_sessions == 0
    assert report.unchanged_sessions == 0
    # session_short has 5 messages, session_tool_blocks has 3 (with tool blocks off),
    # session_malformed has 3 valid lines after skipping 2 bad
    assert _session_count(db_conn) == 3
    assert _message_count(db_conn, "session_short") == 5


def test_incremental_skips_unchanged_files(archive_dir, db_conn):
    """Re-running index with no file changes reports all sessions unchanged."""
    indexer.run_index(db_conn, archive_dir)
    report = indexer.run_index(db_conn, archive_dir)
    assert report.new_sessions == 0
    assert report.updated_sessions == 0
    assert report.unchanged_sessions == 3


def test_changed_file_triggers_reindex(archive_dir, db_conn):
    """Bumping a file's mtime causes re-index of just that session."""
    indexer.run_index(db_conn, archive_dir)
    target = archive_dir / "test-project" / "session_short.jsonl"
    future = time.time() + 60
    os.utime(target, (future, future))
    report = indexer.run_index(db_conn, archive_dir)
    assert report.updated_sessions == 1
    assert report.unchanged_sessions == 2


def test_malformed_line_does_not_crash(archive_dir, db_conn):
    """Malformed JSONL lines are skipped with a count, index continues."""
    report = indexer.run_index(db_conn, archive_dir)
    assert report.malformed_lines >= 2  # 2 bad lines in session_malformed.jsonl
    # The first and last valid lines should have made it through
    rows = db_conn.execute(
        "SELECT content FROM messages WHERE session_id=? ORDER BY turn_index",
        ("session_malformed",),
    ).fetchall()
    assert len(rows) == 3
    assert "First line is valid" in rows[0]["content"]
    assert "Last valid line" in rows[-1]["content"]


def test_rebuild_replaces_cleanly(archive_dir, db_conn):
    """--rebuild drops and re-inserts without orphaned rows."""
    indexer.run_index(db_conn, archive_dir)
    first_msg_count = _message_count(db_conn)
    report = indexer.run_index(db_conn, archive_dir, rebuild=True)
    assert report.new_sessions == 3
    assert report.unchanged_sessions == 0
    assert _message_count(db_conn) == first_msg_count


def test_tool_blocks_skipped_by_default(archive_dir, db_conn):
    """tool_use / tool_result blocks are not indexed unless opt-in."""
    indexer.run_index(db_conn, archive_dir)
    rows = db_conn.execute(
        "SELECT content FROM messages WHERE session_id=? ORDER BY turn_index",
        ("session_tool_blocks",),
    ).fetchall()
    # Default: 3 text-bearing messages (the all-tool-result line is dropped).
    assert len(rows) == 3
    concatenated = "\n".join(r["content"] for r in rows)
    assert "tool_use" not in concatenated
    assert "=== 45 passed" not in concatenated


def test_tool_blocks_included_when_opted_in(archive_dir, db_conn):
    """index_tool_blocks=True includes tool_use and tool_result text."""
    indexer.run_index(db_conn, archive_dir, index_tool_blocks=True)
    rows = db_conn.execute(
        "SELECT content FROM messages WHERE session_id=? ORDER BY turn_index",
        ("session_tool_blocks",),
    ).fetchall()
    assert len(rows) == 4
    concatenated = "\n".join(r["content"] for r in rows)
    assert "tool_use" in concatenated
    assert "45 passed" in concatenated


def test_project_slug_scopes_walk(archive_dir, db_conn):
    """--project scopes the indexer to one project directory."""
    # Create a second project directory that should be ignored.
    other = archive_dir / "other-project"
    other.mkdir()
    (other / "extra.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"ignored"},'
        '"timestamp":"2026-04-21T00:00:00Z"}\n',
        encoding="utf-8",
    )
    report = indexer.run_index(db_conn, archive_dir, project_slug="test-project")
    assert report.new_sessions == 3
    slugs = {
        row["project_slug"]
        for row in db_conn.execute("SELECT DISTINCT project_slug FROM sessions")
    }
    assert slugs == {"test-project"}


def test_run_index_missing_archive_raises(tmp_path, db_conn):
    """Missing archive root raises IndexerError."""
    with pytest.raises(indexer.IndexerError):
        indexer.run_index(db_conn, tmp_path / "does-not-exist")


def test_parse_jsonl_line_malformed_returns_none():
    assert indexer.parse_jsonl_line("not json at all") is None
    assert indexer.parse_jsonl_line('{"incomplete": ') is None


def test_parse_jsonl_line_string_content():
    line = '{"type":"user","message":{"role":"user","content":"hello"}}'
    msg = indexer.parse_jsonl_line(line)
    assert msg is not None
    assert msg["role"] == "user"
    assert msg["content"] == "hello"


def test_parse_jsonl_line_empty_content_returns_none():
    line = '{"type":"user","message":{"role":"user","content":""}}'
    assert indexer.parse_jsonl_line(line) is None


def test_parse_jsonl_line_whitespace_only_returns_none():
    line = '{"type":"user","message":{"role":"user","content":"   \\n  "}}'
    assert indexer.parse_jsonl_line(line) is None


def test_parse_jsonl_line_tool_only_content_returns_none_by_default():
    line = (
        '{"type":"user","message":{"role":"user",'
        '"content":[{"type":"tool_result","tool_use_id":"x","content":"out"}]}}'
    )
    assert indexer.parse_jsonl_line(line) is None
    # But opted-in, it comes back as a single message.
    msg = indexer.parse_jsonl_line(line, index_tool_blocks=True)
    assert msg is not None
    assert "tool_result" in msg["content"]


def test_session_timestamps_recorded(archive_dir, db_conn):
    """started_at and ended_at reflect the first and last message timestamps."""
    indexer.run_index(db_conn, archive_dir)
    row = db_conn.execute(
        "SELECT started_at, ended_at FROM sessions WHERE session_id=?",
        ("session_short",),
    ).fetchone()
    assert row["started_at"] == "2026-04-21T14:32:10Z"
    assert row["ended_at"] == "2026-04-21T14:34:00Z"
