"""Tests for claude_recall.search.

See docs/PLAN.md section 11 for the full test plan.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from claude_recall import indexer, search


@pytest.fixture
def indexed_db(archive_dir, db_conn):
    """DB indexed against the fixture archive."""
    indexer.run_index(db_conn, archive_dir)
    return db_conn


def test_fts5_ranking_order(indexed_db):
    """Known corpus produces the expected top result for a known query."""
    resp = search.run_search(indexed_db, "regex")
    assert resp.total_matches >= 1
    top = resp.results[0]
    # The first match should be the user's question containing "regex"
    assert "regex" in top.content_preview.lower()
    # Scores are ordered high to low
    scores = [r.score for r in resp.results]
    assert scores == sorted(scores, reverse=True)


def test_threshold_filters_low_score(indexed_db):
    """A high threshold drops all low-scoring matches."""
    baseline = search.run_search(indexed_db, "regex")
    assert baseline.total_matches >= 1
    high = search.run_search(indexed_db, "regex", threshold=999.0)
    assert high.total_matches == 0
    assert high.results == []


def test_snippet_marks_matches(indexed_db):
    """Snippets contain <mark>...</mark> around matched terms."""
    resp = search.run_search(indexed_db, "regex")
    assert resp.results
    assert any(
        "<mark>" in r.snippet and "</mark>" in r.snippet for r in resp.results
    )


def test_days_filter(indexed_db):
    """--days N excludes sessions older than the window."""
    # Fixtures are dated 2026-04-21/22; current date is 2026-04-23.
    # days=0 → cutoff is now, which is newer than fixture timestamps → 0 results.
    resp = search.run_search(indexed_db, "regex", days=0)
    assert resp.total_matches == 0
    # Large window returns the baseline hits
    resp_wide = search.run_search(indexed_db, "regex", days=365)
    assert resp_wide.total_matches >= 1


def test_project_scope(indexed_db, archive_dir):
    """--project limits results to one project slug."""
    resp = search.run_search(indexed_db, "regex", project_slug="test-project")
    assert resp.total_matches >= 1
    resp_empty = search.run_search(
        indexed_db, "regex", project_slug="not-a-real-project"
    )
    assert resp_empty.total_matches == 0


def test_malformed_query_falls_back(indexed_db):
    """A query with FTS5 special characters does not crash; falls back to tokens."""
    # Unmatched paren and punctuation would normally fail the raw parse.
    resp = search.run_search(indexed_db, "regex (patterns")
    assert resp.total_matches >= 1


def test_extract_keywords_flag_strips_filler_tokens(indexed_db):
    """extract_keywords=True strips stopwords before FTS5 sees the query.

    With extraction, a natural-language prompt resolves to a clean OR of
    topical tokens. The top BM25 result should be the message that most
    directly matches the topical words.
    """
    resp = search.run_search(
        indexed_db,
        "remind me what we decided about regex patterns",
        extract_keywords=True,
    )
    assert resp.total_matches >= 1
    preview = resp.results[0].content_preview.lower()
    assert "regex" in preview or "patterns" in preview


def test_extract_keywords_off_preserves_raw_query(indexed_db):
    """Direct CLI users with raw queries should see exact FTS5 semantics."""
    # Raw "regex" is an exact FTS5 token; extraction is off by default.
    raw = search.run_search(indexed_db, "regex")
    assert raw.total_matches >= 1
    # No keyword extraction mutation happened — the response echoes the input.
    assert raw.query == "regex"


def test_natural_language_query_zero_hit_fallback(indexed_db):
    """A natural-language prompt whose AND-join finds nothing falls back to OR-joined tokens.

    This is the v0.1.1 stepping-stone for issue #1 — raw FTS5 AND-joins every
    token in the query, which almost never co-occur in a single message. The
    sanitized OR-joined fallback now fires on zero hits, not only on parse
    failure, so natural-language prompts return useful matches.
    """
    resp = search.run_search(
        indexed_db, "remind me what we decided about regex patterns"
    )
    assert resp.total_matches >= 1
    preview = resp.results[0].content_preview.lower()
    assert "regex" in preview or "patterns" in preview


def test_days_filter_uses_message_timestamp_not_session_started_at(
    archive_dir, db_conn
):
    """Issue #13: `--days N` must filter on each message's own timestamp,
    not on the session's `started_at`. A long-lived session whose first
    message is older than the window can still contain recent messages
    that should surface in scoped search.

    Repro from OC's CrewTrack archive: 5,622-turn session spanning many
    months, default `--days 90`, scoped search returns 0 despite many
    recent matches in the unscoped result. Pre-fix the predicate was
    `s.started_at >= ?` (excluded the whole session); post-fix it's
    `m.timestamp >= ?` (filters individual messages).
    """
    project_dir = archive_dir / "test-project"
    long_session = project_dir / "session_long_lived.jsonl"

    today = datetime.now(UTC)
    long_ago = today - timedelta(days=180)
    recent = today - timedelta(days=5)

    # Synthetic JSONL: same shape the fixture sessions use, with timestamps
    # that straddle a 30-day window.
    lines = [
        json.dumps({
            "type": "user",
            "message": {"role": "user",
                        "content": "ancient discussion of zebrafish patterns"},
            "timestamp": long_ago.isoformat().replace("+00:00", "Z"),
            "sessionId": "session_long_lived",
        }),
        json.dumps({
            "type": "user",
            "message": {"role": "user",
                        "content": "recent quokka regex update"},
            "timestamp": recent.isoformat().replace("+00:00", "Z"),
            "sessionId": "session_long_lived",
        }),
    ]
    long_session.write_text("\n".join(lines) + "\n", encoding="utf-8")

    indexer.run_index(db_conn, archive_dir)

    # Sanity-check the test setup: session.started_at IS older than the
    # 30-day cutoff. If this assertion ever fails, the test is no longer
    # exercising the fix.
    row = db_conn.execute(
        "SELECT started_at FROM sessions WHERE session_id=?",
        ("session_long_lived",),
    ).fetchone()
    cutoff_30 = (today - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    assert row["started_at"] < cutoff_30, (
        "test fixture didn't end up with an old started_at; can't exercise #13"
    )

    # Recent message surfaces with --days 30 + project filter (the failing
    # case pre-fix).
    resp = search.run_search(
        db_conn, "quokka", days=30, project_slug="test-project"
    )
    assert resp.total_matches == 1, (
        f"recent message in long-lived session should surface within --days 30 "
        f"window post-fix; got {resp.total_matches} match(es)"
    )
    assert "quokka" in resp.results[0].content_preview.lower()

    # Old message correctly excluded by --days 30.
    resp_old = search.run_search(
        db_conn, "zebrafish", days=30, project_slug="test-project"
    )
    assert resp_old.total_matches == 0

    # And both surface with a wide window (sanity that the data is there).
    resp_wide = search.run_search(
        db_conn, "zebrafish OR quokka", days=365, project_slug="test-project"
    )
    assert resp_wide.total_matches >= 1


def test_unparseable_query_raises(indexed_db):
    """A query with no usable tokens raises SearchError."""
    with pytest.raises(search.SearchError):
        search.run_search(indexed_db, "()!!!")


def test_limit_caps_returned(indexed_db):
    """--limit caps the number of returned results without changing total_matches."""
    full = search.run_search(indexed_db, "tests")
    if full.total_matches < 2:
        pytest.skip("not enough matches in fixture to test limit")
    capped = search.run_search(indexed_db, "tests", limit=1)
    assert capped.returned == 1
    assert capped.total_matches == full.total_matches


def test_format_json_shape(indexed_db):
    """--format json emits the schema documented in PLAN §6.2."""
    resp = search.run_search(indexed_db, "regex")
    body = search.format_result(resp, format="json")
    payload = json.loads(body)
    assert payload["query"] == "regex"
    assert payload["returned"] == len(resp.results)
    assert payload["total_matches"] == resp.total_matches
    assert "threshold" in payload
    for r in payload["results"]:
        assert set(r.keys()) >= {
            "session_id",
            "project_slug",
            "started_at",
            "turn_index",
            "role",
            "score",
            "snippet",
            "content_preview",
        }


def test_format_agent_context_with_results(indexed_db):
    """Issue #21 (v0.6.4): --agent-context emits the wrapped Claude Code
    hook envelope: ``{"hookSpecificOutput": {"hookEventName":
    "UserPromptSubmit", "additionalContext": "..."}}``. The legacy
    top-level ``{"additionalContext": ...}`` form is silently dropped
    by Claude Code's strict-validation pass; the wrapped form is the
    canonical schema-correct shape.
    """
    resp = search.run_search(indexed_db, "regex")
    body = search.format_result(resp, format="agent-context")
    payload = json.loads(body)

    # Wrapped envelope shape.
    assert "hookSpecificOutput" in payload, (
        f"top-level wrapper missing — Claude Code's strict validator drops "
        f"legacy top-level shapes silently: {payload!r}"
    )
    assert "additionalContext" not in payload, (
        f"legacy top-level additionalContext present — should be wrapped "
        f"under hookSpecificOutput: {payload!r}"
    )

    inner = payload["hookSpecificOutput"]
    assert inner.get("hookEventName") == "UserPromptSubmit"
    assert "regex" in inner["additionalContext"].lower()
    # <mark> wrappers are stripped before injection.
    assert "<mark>" not in inner["additionalContext"]


def test_format_agent_context_empty_when_no_results(indexed_db):
    """--agent-context is literally '{}' when no results — hook contract.
    Empty payload is the no-op signal regardless of the wrapped vs legacy
    shape, so this assertion stays the same across v0.6.4.
    """
    resp = search.run_search(indexed_db, "regex", threshold=999.0)
    assert search.format_result(resp, format="agent-context") == "{}"


def test_format_text_no_results(indexed_db):
    """Text format prints a no-match line rather than blank output."""
    resp = search.run_search(indexed_db, "regex", threshold=999.0)
    body = search.format_result(resp, format="text")
    assert "No matches" in body


def test_format_text_with_results(indexed_db):
    resp = search.run_search(indexed_db, "regex")
    body = search.format_result(resp, format="text")
    assert "query:" in body
    assert "returned:" in body


def test_score_is_positive_for_matches(indexed_db):
    """BM25 scores are negated so higher = more relevant."""
    resp = search.run_search(indexed_db, "regex")
    assert resp.results
    assert all(r.score > 0 for r in resp.results)


def test_recent_session_within_days_window(archive_dir, db_conn):
    """A session timestamped within the window is included by days filter."""
    # Append a fresh session file stamped at "now" so the days=1 window includes it.
    now = datetime.now(UTC).isoformat()
    just_past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    line = (
        '{"type":"user","message":{"role":"user",'
        '"content":"fresh unique-token xyzzy token"},'
        f'"timestamp":"{just_past}"}}\n'
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":"acknowledged unique-token xyzzy token"},'
        f'"timestamp":"{now}"}}\n'
    )
    fresh_path = archive_dir / "test-project" / "session_fresh.jsonl"
    fresh_path.write_text(line, encoding="utf-8")
    indexer.run_index(db_conn, archive_dir)
    resp = search.run_search(db_conn, "xyzzy", days=1)
    assert resp.total_matches >= 1


# ---------------------------------------------------------------------------
# v0.8 (issue #27): --kind flag scoping
# ---------------------------------------------------------------------------

def test_kind_filter_scopes_to_requested_kinds(db_conn):
    """`kinds=['THOUGHT']` restricts results to THOUGHT messages.
    A search across a mix of kinds with a shared FTS term should
    return only the kind(s) requested."""
    # Seed a session and four messages with the same matchable token but
    # different content_kind values.
    db_conn.execute(
        "INSERT INTO sessions(session_id, project_slug, file_path, "
        "file_mtime, started_at, ended_at, turn_count, indexed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("s-kind", "p-kind", "/test/s-kind.jsonl", 1.0,
         "2026-04-25", "2026-04-25", 4, "2026-04-25"),
    )
    seeds = [
        ("user", "alpha-token substantive thought content", 0, "THOUGHT"),
        ("assistant", "alpha-token Let me check the patterns", 1, "PROCEDURAL"),
        ("user", "alpha-token <ide_opened_file>foo</ide_opened_file>", 2, "HARNESS"),
        ("assistant", "alpha-token public class { } private string", 3,
         "TOOL_RESULT_EMBEDDED"),
    ]
    for role, content, turn, kind in seeds:
        db_conn.execute(
            "INSERT INTO messages(session_id, role, content, turn_index, "
            "timestamp, content_hash, content_kind) "
            "VALUES (?,?,?,?,?,?,?)",
            ("s-kind", role, content, turn, "2026-04-25", "h", kind),
        )
    db_conn.commit()

    # Default: no --kind, all four hits returned.
    all_kinds = search.run_search(db_conn, "alpha-token", days=365, limit=10)
    assert all_kinds.total_matches == 4

    # --kind THOUGHT: only the THOUGHT message.
    thought_only = search.run_search(
        db_conn, "alpha-token", days=365, limit=10, kinds=["THOUGHT"],
    )
    assert thought_only.total_matches == 1
    assert "substantive" in thought_only.results[0].content_preview

    # --kind THOUGHT --kind PROCEDURAL: both, but not HARNESS or TOOL.
    two_kinds = search.run_search(
        db_conn, "alpha-token", days=365, limit=10,
        kinds=["THOUGHT", "PROCEDURAL"],
    )
    assert two_kinds.total_matches == 2


def test_kind_filter_includes_null_kind_rows(db_conn):
    """NULL content_kind is included alongside any requested kinds —
    same NULL-tolerance discipline `topics` uses, so partially-migrated
    DBs don't silently lose hits."""
    db_conn.execute(
        "INSERT INTO sessions(session_id, project_slug, file_path, "
        "file_mtime, started_at, ended_at, turn_count, indexed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("s-null", "p-null", "/test/s-null.jsonl", 1.0,
         "2026-04-25", "2026-04-25", 2, "2026-04-25"),
    )
    db_conn.execute(
        "INSERT INTO messages(session_id, role, content, turn_index, "
        "timestamp, content_hash, content_kind) "
        "VALUES (?,?,?,?,?,?,?)",
        ("s-null", "user", "betatok thought content", 0, "2026-04-25", "h",
         "THOUGHT"),
    )
    db_conn.execute(
        "INSERT INTO messages(session_id, role, content, turn_index, "
        "timestamp, content_hash, content_kind) "
        "VALUES (?,?,?,?,?,?,?)",
        ("s-null", "user", "betatok unclassified content", 1, "2026-04-25", "h",
         None),
    )
    db_conn.commit()

    # --kind THOUGHT: returns BOTH (THOUGHT row + NULL-kind row).
    resp = search.run_search(
        db_conn, "betatok", days=365, limit=10, kinds=["THOUGHT"],
    )
    assert resp.total_matches == 2
