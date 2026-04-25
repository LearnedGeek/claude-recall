"""Tests for claude_recall.indexer.

See docs/PLAN.md section 11 for the full test plan.
"""

import json
import os
import threading
import time

import pytest

from claude_recall import indexer, storage


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
    """Bumping a file's mtime causes re-evaluation of the session.

    Under v0.6 hash-diff logic, mtime change without content change is
    correctly reported as unchanged_sessions (the parse + diff happens but
    finds nothing different to update — no DB writes occur). The mtime
    update on sessions.file_mtime IS persisted so the fast path engages
    next time. To assert the slow-path-was-entered specifically, see
    test_mtime_jitter_without_content_change_is_noop in v0.6 regression
    tests.
    """
    indexer.run_index(db_conn, archive_dir)
    target = archive_dir / "test-project" / "session_short.jsonl"
    future = time.time() + 60
    os.utime(target, (future, future))
    report = indexer.run_index(db_conn, archive_dir)
    assert report.updated_sessions == 0
    assert report.incremental_sessions == 0
    assert report.unchanged_sessions == 3


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


# =============================================================================
# v0.6 — content-hash-aware indexer regression tests (issue #16)
#
# These tests exercise the architectural fix in v0.6 where the indexer no
# longer cascade-wipes vectors on routine re-ingest. Each test seeds dummy
# vectors via _seed_dummy_vectors(), mutates the JSONL on disk to simulate
# a real-world scenario (append, edit, compaction, etc.), re-indexes, and
# asserts which vectors survived the cascade. Bytes-equality on the
# vector blob is the proof that the original row was untouched (vs
# DELETE+reinsert which would have produced a fresh row).
# =============================================================================


def _seed_dummy_vectors(conn, session_id: str) -> dict[int, bytes]:
    """Insert a dummy vector for every existing message in the session.

    Returns {msg_id: vector_bytes} so tests can verify byte-for-byte
    preservation across re-index. Each vector is unique-per-msg_id so a
    later read can prove which specific row survived.
    """
    msg_rows = conn.execute(
        "SELECT msg_id FROM messages WHERE session_id=? ORDER BY turn_index",
        (session_id,),
    ).fetchall()
    inserted: dict[int, bytes] = {}
    for row in msg_rows:
        msg_id = row["msg_id"]
        # Distinguishable per-msg: 32 bytes encoding the msg_id.
        vec = msg_id.to_bytes(4, "little") + bytes(28)
        conn.execute(
            "INSERT INTO message_vectors(msg_id, vector, model, dim, embedded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (msg_id, vec, "test-model", 32, "2026-04-25T00:00:00Z"),
        )
        inserted[msg_id] = vec
    conn.commit()
    return inserted


def _surviving_vectors(conn) -> dict[int, bytes]:
    rows = conn.execute("SELECT msg_id, vector FROM message_vectors").fetchall()
    return {r["msg_id"]: bytes(r["vector"]) for r in rows}


def _make_turn_line(role: str, content: str, ts: str) -> str:
    """Construct a JSONL line in the shape the parser expects."""
    return json.dumps({
        "type": role,
        "message": {"role": role, "content": content},
        "timestamp": ts,
        "sessionId": "fixture-test",
    }) + "\n"


def test_append_only_normal_case_preserves_existing_vectors(archive_dir, db_conn):
    """Append a turn → all original (msg_id, vector) pairs unchanged, new
    message inserted at next turn_index with no vector. The most common
    real-world scenario (every Claude Code turn touches the JSONL).
    """
    indexer.run_index(db_conn, archive_dir)
    seeded = _seed_dummy_vectors(db_conn, "session_short")
    assert len(seeded) == 5

    target = archive_dir / "test-project" / "session_short.jsonl"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(_make_turn_line(
            "user", "One more thing — what's the rollback plan?",
            "2026-04-21T14:35:00Z",
        ))
    # Force mtime change so indexer takes the slow path.
    future = time.time() + 60
    os.utime(target, (future, future))

    report = indexer.run_index(db_conn, archive_dir)
    assert report.incremental_sessions == 1

    surviving = _surviving_vectors(db_conn)
    # Every original (msg_id, vector) pair survives byte-for-byte.
    for msg_id, vec in seeded.items():
        assert msg_id in surviving, f"vector for msg_id {msg_id} was wiped"
        assert surviving[msg_id] == vec, (
            f"vector for msg_id {msg_id} was rewritten — should have been untouched"
        )
    # New 6th message exists with no vector yet.
    msg_count = db_conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=?", ("session_short",),
    ).fetchone()[0]
    assert msg_count == 6
    assert len(surviving) == 5  # Still 5 vectors; the new 6th has none.


def test_compaction_event_replaces_changed_indices_preserves_unchanged(
    archive_dir, db_conn
):
    """Issue #16 + DC's compaction-test scenario: rewrite the JSONL so that
    early turns are replaced with a summary turn. The v0.6 turn_index-based
    diff correctly DELETEs vectors for old content at the changed indices
    and INSERTs new rows for the summary. Vectors at unchanged turn_indices
    (where new content equals old content at the same position) survive.

    Note: this test asserts the v0.6 turn_index-based diff contract, not the
    hash-based reuse contract. If Claude Code's actual compaction shifts
    tail content to lower turn_indices (rather than rewriting all early
    indices), some tail vectors will be lost — recoverable via
    `claude-recall embed`. Hash-based reuse is a v0.7 candidate.
    """
    indexer.run_index(db_conn, archive_dir)
    seeded = _seed_dummy_vectors(db_conn, "session_short")
    assert len(seeded) == 5

    target = archive_dir / "test-project" / "session_short.jsonl"
    # Original session_short has 5 turns. Simulate a compaction event:
    # the file now has 3 lines: [summary, original turn 4, original turn 5].
    original_lines = target.read_text(encoding="utf-8").splitlines()
    summary = _make_turn_line(
        "system",
        "[compacted summary of turns 0-2]",
        "2026-04-21T14:32:10Z",
    )
    target.write_text(
        summary + original_lines[3] + "\n" + original_lines[4] + "\n",
        encoding="utf-8",
    )
    future = time.time() + 60
    os.utime(target, (future, future))

    report = indexer.run_index(db_conn, archive_dir)
    assert report.incremental_sessions == 1

    surviving = _surviving_vectors(db_conn)
    # The session now has 3 turns. Old turns 0-2 (summary, replacing them)
    # and old turns 3-4 (now at turn_indices 1-2 — different content from
    # original turn_indices 1-2 → will be DELETEd as "content changed at
    # this turn_index"). The summary turn at index 0 is also content-
    # changed vs original turn 0. So all 5 original vectors die.
    # If you're reading this and confused why all 5 die: the test asserts
    # the simpler turn_index diff. Hash-based reuse would preserve 2 of 5
    # (the tail content matched at shifted positions); deferred to v0.7.
    for old_msg_id in seeded:
        assert old_msg_id not in surviving, (
            f"vector for old msg_id {old_msg_id} survived — content changed "
            f"at its turn_index, should have cascaded out"
        )
    # The 3 new messages have no vectors yet (will be embedded on next run).
    assert len(surviving) == 0


def test_edit_mid_stream_deletes_only_affected_turn_vector(
    archive_dir, db_conn
):
    """A mid-stream content edit should DELETE only the affected msg_id.
    All other vectors survive byte-for-byte.
    """
    indexer.run_index(db_conn, archive_dir)
    seeded = _seed_dummy_vectors(db_conn, "session_short")
    assert len(seeded) == 5

    # Map turn_index → msg_id so we can identify which vector should die.
    rows = db_conn.execute(
        "SELECT msg_id, turn_index FROM messages WHERE session_id=? "
        "ORDER BY turn_index",
        ("session_short",),
    ).fetchall()
    msg_id_at = {r["turn_index"]: r["msg_id"] for r in rows}

    # Rewrite turn 2 with different content. Keep all other lines verbatim.
    target = archive_dir / "test-project" / "session_short.jsonl"
    original_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    edited_turn_2 = _make_turn_line(
        "user",
        "Actually, let me change the question entirely.",
        "2026-04-21T14:33:02Z",
    )
    new_content = (
        original_lines[0] + original_lines[1] + edited_turn_2
        + original_lines[3] + original_lines[4]
    )
    target.write_text(new_content, encoding="utf-8")
    future = time.time() + 60
    os.utime(target, (future, future))

    indexer.run_index(db_conn, archive_dir)

    surviving = _surviving_vectors(db_conn)
    # The vector for turn_index=2 should be gone (content changed there).
    assert msg_id_at[2] not in surviving
    # Vectors for turn_indices 0, 1, 3, 4 should survive byte-for-byte.
    for ti in (0, 1, 3, 4):
        msg_id = msg_id_at[ti]
        assert msg_id in surviving
        assert surviving[msg_id] == seeded[msg_id]


def test_mtime_jitter_without_content_change_is_noop(archive_dir, db_conn):
    """Touch mtime without changing content → hash diff sees no changes,
    no DELETEs, no INSERTs, all vectors survive. Slow path entered but
    produces zero DB writes.
    """
    indexer.run_index(db_conn, archive_dir)
    seeded = _seed_dummy_vectors(db_conn, "session_short")
    assert len(seeded) == 5

    target = archive_dir / "test-project" / "session_short.jsonl"
    future = time.time() + 60
    os.utime(target, (future, future))

    report = indexer.run_index(db_conn, archive_dir)
    # All 3 fixture sessions should be unchanged_sessions (the touched
    # one took the slow path but found no changes; the other two took the
    # mtime fast path).
    assert report.unchanged_sessions == 3
    assert report.incremental_sessions == 0
    assert report.updated_sessions == 0

    surviving = _surviving_vectors(db_conn)
    for msg_id, vec in seeded.items():
        assert msg_id in surviving
        assert surviving[msg_id] == vec


def test_malformed_line_appended_does_not_break_session(archive_dir, db_conn):
    """Append a malformed line followed by a valid line. Malformed gets
    skipped (counted in malformed_lines); valid lines past it are ingested
    correctly. Existing vectors for prior content still survive.
    """
    indexer.run_index(db_conn, archive_dir)
    seeded = _seed_dummy_vectors(db_conn, "session_short")

    target = archive_dir / "test-project" / "session_short.jsonl"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("{this is not valid json\n")
        fh.write(_make_turn_line("user", "Recovered after a bad line.",
                                 "2026-04-21T14:40:00Z"))
    future = time.time() + 60
    os.utime(target, (future, future))

    report = indexer.run_index(db_conn, archive_dir)
    assert report.malformed_lines >= 1

    # The valid 6th message landed.
    rows = db_conn.execute(
        "SELECT content FROM messages WHERE session_id=? ORDER BY turn_index",
        ("session_short",),
    ).fetchall()
    assert any("Recovered after a bad line" in r["content"] for r in rows)

    # Original 5 vectors survive.
    surviving = _surviving_vectors(db_conn)
    for msg_id, vec in seeded.items():
        assert msg_id in surviving
        assert surviving[msg_id] == vec


def test_v1_to_v2_migration_preserves_vectors(tmp_path):
    """Open a manually-constructed v1-schema DB (with embedded vectors),
    let storage.open_db() auto-migrate, assert content_hash is populated
    on every row, vectors are still joinable by msg_id, schema_version
    is 2.

    This is the load-bearing migration test — anyone upgrading from v0.5.x
    must keep their existing vectors.
    """
    import sqlite3 as _sqlite
    db_path = tmp_path / "v1.db"

    # Build a v1-schema DB by hand (no content_hash column, schema_version=1).
    # IMPORTANT: include the FTS5 virtual table and its triggers — real v0.5
    # DBs in the wild always have these, and the migration's UPDATE backfill
    # fires the AFTER UPDATE trigger which writes to messages_fts. If
    # messages_fts isn't set up, SQLite reports "database disk image is
    # malformed" when the trigger tries to update an FTS5 table the existing
    # rows aren't in.
    conn = _sqlite.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            project_slug TEXT NOT NULL,
            file_path TEXT NOT NULL UNIQUE,
            file_mtime REAL NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            turn_count INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            timestamp TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                ON DELETE CASCADE
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content,
            content='messages',
            content_rowid='msg_id',
            tokenize='porter unicode61'
        );
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content)
                VALUES (new.msg_id, new.content);
        END;
        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES ('delete', old.msg_id, old.content);
        END;
        CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES ('delete', old.msg_id, old.content);
            INSERT INTO messages_fts(rowid, content)
                VALUES (new.msg_id, new.content);
        END;
        CREATE TABLE message_vectors (
            msg_id INTEGER PRIMARY KEY,
            vector BLOB NOT NULL,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            embedded_at TEXT NOT NULL,
            FOREIGN KEY (msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE
        );
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (1);
    """)
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
        ("s1", "p1", "/tmp/s1.jsonl", 1.0, "ts0", "ts1", 2,
         "2026-04-25T00:00:00Z"),
    )
    # Inserts AFTER triggers are in place, so FTS5 auto-populates.
    conn.execute(
        "INSERT INTO messages(session_id, role, content, turn_index, timestamp) "
        "VALUES (?,?,?,?,?)",
        ("s1", "user", "hello world", 0, "ts0"),
    )
    conn.execute(
        "INSERT INTO messages(session_id, role, content, turn_index, timestamp) "
        "VALUES (?,?,?,?,?)",
        ("s1", "assistant", "hi there", 1, "ts1"),
    )
    conn.execute(
        "INSERT INTO message_vectors VALUES (?,?,?,?,?)",
        (1, b"vector_for_msg_1_padded_to_32xx", "test", 32,
         "2026-04-25T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO message_vectors VALUES (?,?,?,?,?)",
        (2, b"vector_for_msg_2_padded_to_32xx", "test", 32,
         "2026-04-25T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    # Open via the v0.6 path — migration should run.
    conn = storage.open_db(db_path)
    try:
        # schema_version bumped to 2.
        rows = conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        versions = [r["version"] for r in rows]
        assert 2 in versions

        # content_hash column exists and is populated for all messages.
        cols = [
            r["name"] for r in conn.execute(
                "PRAGMA table_info(messages)"
            ).fetchall()
        ]
        assert "content_hash" in cols
        rows = conn.execute(
            "SELECT msg_id, content, content_hash FROM messages "
            "WHERE session_id=? ORDER BY turn_index",
            ("s1",),
        ).fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r["content_hash"] is not None
            # The hash must equal the canonical hash function applied to the
            # content. Anyone re-implementing should see this assertion fail.
            assert r["content_hash"] == storage.content_hash(r["content"])

        # Vectors still joinable by msg_id (FK still valid; nothing cascade-
        # deleted them during migration).
        joined = conn.execute("""
            SELECT m.msg_id, m.content, v.vector
            FROM messages m JOIN message_vectors v USING (msg_id)
            WHERE m.session_id = ? ORDER BY m.turn_index
        """, ("s1",)).fetchall()
        assert len(joined) == 2
        assert joined[0]["msg_id"] == 1
        assert joined[1]["msg_id"] == 2
    finally:
        conn.close()


def test_concurrent_index_runs_do_not_double_insert(archive_dir, tmp_path):
    """Two threads call run_index against the same archive simultaneously
    — BEGIN IMMEDIATE serializes the per-file diff so neither doubles up
    rows. After both finish, message counts equal one-pass counts.
    """
    db_path = tmp_path / "concurrent.db"

    # Each thread gets its own connection (sqlite3 connections are not
    # thread-safe by default).
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker():
        try:
            conn = storage.open_db(db_path)
            try:
                barrier.wait(timeout=5)
                indexer.run_index(conn, archive_dir)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not errors, f"worker errors: {errors}"

    # Verify exactly one set of messages — no doubling.
    conn = storage.open_db(db_path)
    try:
        single_session_msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?",
            ("session_short",),
        ).fetchone()[0]
        assert single_session_msg_count == 5, (
            f"expected 5 messages for session_short after concurrent indexing, "
            f"got {single_session_msg_count} — BEGIN IMMEDIATE didn't serialize"
        )
        # Total across all 3 fixture sessions should match a single-pass index.
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert total == 11  # 5 + 3 + 3 (matches test_index_fresh_archive shape)
    finally:
        conn.close()


def test_full_lifecycle_index_embed_append_index_preserves_coverage(
    archive_dir, db_conn
):
    """End-to-end: fresh index → seed dummy vectors (simulating embed) →
    append a turn → re-index. After the cycle, vectors_indexed equals the
    pre-append count + 0 (the new turn isn't embedded yet); the existing
    vectors all survived. This is the 'normal day' regression test.
    """
    indexer.run_index(db_conn, archive_dir)

    pre_msg_count = db_conn.execute(
        "SELECT COUNT(*) FROM messages"
    ).fetchone()[0]
    seeded = _seed_dummy_vectors(db_conn, "session_short")
    seeded.update(_seed_dummy_vectors(db_conn, "session_malformed"))
    seeded.update(_seed_dummy_vectors(db_conn, "session_tool_blocks"))
    pre_vector_count = db_conn.execute(
        "SELECT COUNT(*) FROM message_vectors"
    ).fetchone()[0]
    assert pre_vector_count == pre_msg_count, "test setup: full coverage"

    target = archive_dir / "test-project" / "session_short.jsonl"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(_make_turn_line(
            "assistant", "Done. Coverage held.",
            "2026-04-21T14:36:00Z",
        ))
    future = time.time() + 60
    os.utime(target, (future, future))

    indexer.run_index(db_conn, archive_dir)

    post_msg_count = db_conn.execute(
        "SELECT COUNT(*) FROM messages"
    ).fetchone()[0]
    post_vector_count = db_conn.execute(
        "SELECT COUNT(*) FROM message_vectors"
    ).fetchone()[0]
    assert post_msg_count == pre_msg_count + 1
    # All original vectors survive; new message has no vector yet.
    assert post_vector_count == pre_vector_count
    # And the original vectors are byte-for-byte unchanged.
    surviving = _surviving_vectors(db_conn)
    for msg_id, vec in seeded.items():
        assert msg_id in surviving
        assert surviving[msg_id] == vec
