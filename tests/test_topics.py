"""Tests for claude_recall.topics (v0.7).

Synthetic-vector tests: we hand-construct float32 arrays whose cosine
relationships are known, insert them into the message_vectors table,
and assert clustering / labeling / scoring behavior. This is faster
and more deterministic than running real Ollama embeddings, and lets
us exercise pathological cases (dim mismatch, empty corpus, etc.) that
real embeddings wouldn't reliably produce.
"""

import json

import numpy as np
import pytest

from claude_recall import embeddings, storage, topics


def _seed_message(conn, *, msg_id, session_id, project_slug, content,
                  vector, dim=8, role="user", started_at="2026-04-25",
                  turn_index=0, content_kind="THOUGHT"):
    """Insert a session row (if needed), a messages row, and a message_vectors
    row with the given vector. Self-contained — no fixture archive required.

    ``content_kind`` defaults to THOUGHT so existing tests (pre-v0.8) that
    don't know about kinds get the kind that ``topics`` actually queries.
    """
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, project_slug, file_path, "
        "file_mtime, started_at, ended_at, turn_count, indexed_at) "
        "VALUES (?, ?, ?, 0, ?, ?, 0, ?)",
        (session_id, project_slug, f"/test/{session_id}.jsonl", started_at,
         started_at, "2026-04-25T00:00:00Z"),
    )
    # Override autoincrement so our msg_id matches the seed.
    conn.execute(
        "INSERT INTO messages(msg_id, session_id, role, content, turn_index, "
        "timestamp, content_hash, content_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (msg_id, session_id, role, content, turn_index, started_at, "",
         content_kind),
    )
    conn.execute(
        "INSERT INTO message_vectors(msg_id, vector, model, dim, embedded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (msg_id, embeddings.pack_vector(np.asarray(vector, dtype=np.float32)),
         "test-model", dim, "2026-04-25T00:00:00Z"),
    )
    conn.commit()


def _cluster_at(direction_idx, dim, jitter=0.05, seed=42):
    """Return a vector mostly pointing along basis direction `direction_idx`
    with a small random jitter. Vectors in the same cluster have high cosine
    to each other; across clusters cosine is near 0.
    """
    rng = np.random.default_rng(seed + direction_idx)
    v = rng.normal(0, jitter, size=dim).astype(np.float32)
    v[direction_idx] += 1.0
    return v


def test_topics_groups_semantically_close_messages_into_one_cluster(db_conn):
    """Three sets of 5 vectors, each set clustered along a different basis
    direction. Expect 3 clusters — no cross-pollution."""
    next_id = 1
    for cluster_idx in range(3):
        for variant in range(5):
            v = _cluster_at(cluster_idx, dim=8, seed=cluster_idx * 100 + variant)
            _seed_message(
                db_conn,
                msg_id=next_id,
                session_id=f"sess-c{cluster_idx}-v{variant}",
                project_slug="proj-A",
                content=f"cluster{cluster_idx} message variant {variant}",
                vector=v,
            )
            next_id += 1

    response = topics.run_topics(
        db_conn,
        similarity_threshold=0.5,  # loose-ish; basis vectors are ~0.99 same-cluster
        min_cluster_size=3,
    )

    assert response.total_messages_clustered == 15
    assert response.total_clusters == 3, (
        f"expected 3 clusters from 3 well-separated sets, got "
        f"{response.total_clusters}"
    )
    assert len(response.themes) == 3
    for theme in response.themes:
        assert theme.cluster_size == 5


def test_topics_label_extracts_distinctive_words(db_conn):
    """Two clusters: one repeats 'regex' and 'patterns', the other repeats
    'deployment' and 'pipeline'. Both have shared filler. Labels should
    contain the distinctive words, not the shared ones."""
    shared_filler = "we discussed working approach team"
    next_id = 1
    for variant in range(5):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"reg-{variant}",
            project_slug="proj-A",
            content=f"{shared_filler} regex patterns regex regex patterns",
            vector=v,
        )
        next_id += 1
    for variant in range(5):
        v = _cluster_at(1, dim=8, seed=100 + variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"dep-{variant}",
            project_slug="proj-B",
            content=f"{shared_filler} deployment pipeline deployment deployment pipeline",
            vector=v,
        )
        next_id += 1

    response = topics.run_topics(
        db_conn, similarity_threshold=0.5, min_cluster_size=3,
    )

    assert response.total_clusters == 2
    labels = [t.label for t in response.themes]
    label_text = " ".join(labels).lower()
    assert "regex" in label_text or "patterns" in label_text, (
        f"distinctive 'regex/patterns' word missing from labels: {labels!r}"
    )
    assert "deployment" in label_text or "pipeline" in label_text, (
        f"distinctive 'deployment/pipeline' word missing from labels: {labels!r}"
    )
    # Shared filler should NOT dominate. "discussed" appears in every cluster
    # so its IDF is zero — it should be absent from labels.
    assert "discussed" not in label_text, (
        f"shared filler 'discussed' present in labels — TF-IDF didn't filter "
        f"corpus-common words: {labels!r}"
    )


def test_topics_score_orders_cross_project_themes_above_single_project(
    db_conn,
):
    """Two clusters of equal size. One spans 4 projects, the other is in
    one project. The cross-project cluster should rank first."""
    next_id = 1
    # Cluster 0: 4 messages across 4 different projects.
    for project_idx in range(4):
        v = _cluster_at(0, dim=8, seed=project_idx)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"cross-{project_idx}",
            project_slug=f"proj-cross-{project_idx}",
            content=f"theme content variant {project_idx}",
            vector=v,
        )
        next_id += 1
    # Cluster 1: 4 messages all in one project.
    for variant in range(4):
        v = _cluster_at(1, dim=8, seed=100 + variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"single-{variant}",
            project_slug="proj-single",
            content=f"other theme variant {variant}",
            vector=v,
        )
        next_id += 1

    response = topics.run_topics(
        db_conn, similarity_threshold=0.5, min_cluster_size=3,
    )

    assert response.total_clusters == 2
    # First theme should be the cross-project one.
    assert response.themes[0].project_count == 4, (
        f"cross-project theme didn't rank first: themes={response.themes!r}"
    )
    assert response.themes[1].project_count == 1
    # Score computation: cluster_size × project_count.
    assert response.themes[0].score == 4 * 4
    assert response.themes[1].score == 4 * 1


def test_topics_returns_empty_when_no_vectors_indexed(db_conn):
    """Empty message_vectors → no exception, empty response."""
    response = topics.run_topics(db_conn)
    assert response.total_messages_clustered == 0
    assert response.total_clusters == 0
    assert response.themes == []
    assert response.noise_count == 0


def test_topics_skips_dim_mismatch_vectors_silently(db_conn):
    """Mixed 8-dim and 4-dim vectors. The first row's dim becomes the
    expected dim; the rest are skipped. No exception, and clustering
    only runs on the conforming subset."""
    next_id = 1
    # 5 conformant 8-dim vectors that should cluster.
    for variant in range(5):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"good-{variant}",
            project_slug="proj-A",
            content=f"valid vector {variant}",
            vector=v,
            dim=8,
        )
        next_id += 1
    # 3 nonconformant 4-dim vectors that should be silently skipped.
    for variant in range(3):
        bad = np.array([1.0, 0, 0, 0], dtype=np.float32)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"bad-{variant}",
            project_slug="proj-A",
            content=f"wrong dim vector {variant}",
            vector=bad,
            dim=4,
        )
        next_id += 1

    response = topics.run_topics(
        db_conn, similarity_threshold=0.5, min_cluster_size=3,
    )

    # Only the 5 conformant vectors got clustered; 3 dim-mismatch vectors
    # are silently dropped (mirrors _semantic_rerank's defense).
    assert response.total_messages_clustered == 5
    assert response.total_clusters == 1
    assert response.themes[0].cluster_size == 5


def test_topics_filters_below_min_cluster_size_to_noise(db_conn):
    """Two singletons + one cluster of 5. With min_cluster_size=4, the
    singletons become noise; only the qualifying cluster produces a theme."""
    next_id = 1
    for variant in range(5):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"big-{variant}",
            project_slug="proj-A",
            content=f"qualifying theme {variant}",
            vector=v,
        )
        next_id += 1
    # Two singletons in unrelated directions (won't merge into the big cluster).
    for direction in (4, 5):
        v = _cluster_at(direction, dim=8, seed=direction * 1000)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"loner-{direction}",
            project_slug="proj-B",
            content=f"isolated message {direction}",
            vector=v,
        )
        next_id += 1

    response = topics.run_topics(
        db_conn, similarity_threshold=0.5, min_cluster_size=4,
    )

    assert response.total_clusters == 1
    assert response.noise_count == 2
    assert len(response.themes) == 1


def test_topics_format_json_matches_dataclass_shape(db_conn):
    """`--format json` emits the full TopicsResponse dataclass structure."""
    next_id = 1
    for variant in range(4):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"sess-{variant}",
            project_slug="proj-A",
            content=f"json shape test {variant}",
            vector=v,
        )
        next_id += 1
    response = topics.run_topics(
        db_conn, similarity_threshold=0.5, min_cluster_size=3,
    )
    body = topics.format_topics(response, format="json")
    payload = json.loads(body)
    assert "total_messages_clustered" in payload
    assert "themes" in payload
    assert payload["themes"][0]["label"]
    assert payload["themes"][0]["cluster_size"] == 4


def test_topics_format_agent_context_wrapped_envelope(db_conn):
    """`--format agent-context` emits the wrapped hookSpecificOutput shape
    (issue #21) so the same machinery as search hooks can pipe topics into
    a manual planning hook."""
    next_id = 1
    for variant in range(4):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"sess-{variant}",
            project_slug="proj-A",
            content=f"agent-context test {variant}",
            vector=v,
        )
        next_id += 1
    response = topics.run_topics(
        db_conn, similarity_threshold=0.5, min_cluster_size=3,
    )
    body = topics.format_topics(response, format="agent-context")
    payload = json.loads(body)
    assert "hookSpecificOutput" in payload
    inner = payload["hookSpecificOutput"]
    assert inner["hookEventName"] == "UserPromptSubmit"
    assert "Recurring themes" in inner["additionalContext"]


def test_topics_agent_context_empty_when_no_themes(db_conn):
    """Empty `{}` when there are no themes — hook contract."""
    response = topics.run_topics(db_conn)
    assert topics.format_topics(response, format="agent-context") == "{}"


def test_topics_since_iso_date_windows_input_to_recent_messages(db_conn):
    """`--since 2026-04-01` should drop messages timestamped before the cutoff
    from the input set before clustering. This isolates the time filter — five
    messages from January and five from late April; only the late-April set
    should make it into a theme."""
    from datetime import UTC, datetime
    next_id = 1
    # Old set: January messages, basis 0.
    for variant in range(5):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"old-{variant}",
            project_slug="proj-A",
            content=f"old message {variant}",
            vector=v,
            started_at=f"2026-01-{15 + variant:02d}T12:00:00+00:00",
        )
        next_id += 1
    # Recent set: late-April messages, basis 1 (different cluster).
    for variant in range(5):
        v = _cluster_at(1, dim=8, seed=100 + variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"new-{variant}",
            project_slug="proj-B",
            content=f"new message {variant}",
            vector=v,
            started_at=f"2026-04-{20 + variant:02d}T12:00:00+00:00",
        )
        next_id += 1

    cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    response = topics.run_topics(
        db_conn, since=cutoff,
        similarity_threshold=0.5, min_cluster_size=3,
    )

    assert response.total_messages_clustered == 5, (
        f"expected only the 5 post-cutoff messages, got "
        f"{response.total_messages_clustered}"
    )
    assert response.total_clusters == 1
    assert response.themes[0].project_slugs == ["proj-B"]
    assert response.since == cutoff.isoformat()


def test_topics_since_shorthand_30d_equivalent_to_iso(db_conn):
    """`parse_since('30d')` should yield the same cutoff as 30 days before
    the reference time. Pinning `now` keeps the test deterministic."""
    from datetime import UTC, datetime, timedelta
    fixed_now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
    via_shorthand = topics.parse_since("30d", now=fixed_now)
    via_iso = topics.parse_since("2026-03-30T12:00:00+00:00", now=fixed_now)
    assert via_shorthand == fixed_now - timedelta(days=30)
    # Same calendar instant, just different spellings.
    assert via_shorthand == via_iso


def test_topics_since_invalid_format_raises_actionable_error(db_conn):
    """`--since garbage` should raise TopicsError with both accepted
    formats named in the message — caller (CLI) maps to exit 2."""
    with pytest.raises(topics.TopicsError) as excinfo:
        topics.parse_since("garbage")
    msg = str(excinfo.value)
    assert "ISO" in msg, f"actionable error must name ISO format: {msg!r}"
    assert "shorthand" in msg.lower(), (
        f"actionable error must name shorthand format: {msg!r}"
    )


def test_topics_since_excludes_null_timestamp_messages(db_conn):
    """Messages whose timestamp is NULL (pre-v0.6 archive rows) fall outside
    any --since window — we can't confidently date them, so they're excluded
    rather than silently included or excluded depending on SQL operator
    semantics."""
    next_id = 1
    # Two messages with explicit recent timestamps.
    for variant in range(2):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"dated-{variant}",
            project_slug="proj-A",
            content=f"dated message {variant}",
            vector=v,
            started_at="2026-04-25T12:00:00+00:00",
        )
        next_id += 1
    # Two messages with NULL timestamp (manually patched after seeding).
    for variant in range(2):
        v = _cluster_at(0, dim=8, seed=10 + variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"undated-{variant}",
            project_slug="proj-A",
            content=f"undated message {variant}",
            vector=v,
            started_at="2026-04-25T12:00:00+00:00",
        )
        db_conn.execute(
            "UPDATE messages SET timestamp = NULL WHERE msg_id = ?", (next_id,)
        )
        db_conn.commit()
        next_id += 1

    from datetime import UTC, datetime
    cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    response = topics.run_topics(
        db_conn, since=cutoff, min_cluster_size=2, similarity_threshold=0.5,
    )

    assert response.total_messages_clustered == 2, (
        f"NULL-timestamp messages should be excluded from a --since window, "
        f"got total_messages_clustered={response.total_messages_clustered}"
    )


def test_topics_since_text_format_header_shows_cutoff_date(db_conn):
    """The text format header should surface `since: YYYY-MM-DD` so users
    immediately see what window they're looking at."""
    next_id = 1
    for variant in range(4):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"sess-{variant}",
            project_slug="proj-A",
            content=f"recent message {variant}",
            vector=v,
            started_at="2026-04-25T12:00:00+00:00",
        )
        next_id += 1

    from datetime import UTC, datetime
    response = topics.run_topics(
        db_conn,
        since=datetime(2026, 4, 1, tzinfo=UTC),
        similarity_threshold=0.5,
        min_cluster_size=3,
    )
    text = topics.format_topics(response, format="text")
    assert "since: 2026-04-01" in text, (
        f"text header missing 'since:' field: {text!r}"
    )


def test_topics_project_filter_scopes_clustering(db_conn):
    """`--project <slug>` filters input messages to one project before
    clustering. Other projects' messages don't contribute."""
    next_id = 1
    for variant in range(5):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"a-{variant}",
            project_slug="proj-target",
            content=f"target project message {variant}",
            vector=v,
        )
        next_id += 1
    for variant in range(5):
        v = _cluster_at(1, dim=8, seed=100 + variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"b-{variant}",
            project_slug="proj-other",
            content=f"other project message {variant}",
            vector=v,
        )
        next_id += 1

    response = topics.run_topics(
        db_conn, project_slug="proj-target",
        similarity_threshold=0.5, min_cluster_size=3,
    )

    assert response.total_messages_clustered == 5
    assert response.total_clusters == 1
    assert response.themes[0].project_slugs == ["proj-target"]
    assert response.project_slug == "proj-target"


# ---------------------------------------------------------------------------
# v0.8 (issue #27): THOUGHT-only filter
# ---------------------------------------------------------------------------

def test_topics_excludes_harness_procedural_tool_result(db_conn):
    """The THOUGHT filter is the load-bearing fix for issue #27. Seed a
    mix of all four kinds and assert ``topics`` returns only the THOUGHT
    cluster — HARNESS, PROCEDURAL, and TOOL_RESULT_EMBEDDED messages
    are pre-classified at index time and skipped by the SQL filter."""
    next_id = 1
    # 5 THOUGHT messages clustering on direction 0.
    for variant in range(5):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"thought-{variant}",
            project_slug="proj-A",
            content=f"substantive design discussion {variant}",
            vector=v,
            content_kind="THOUGHT",
        )
        next_id += 1
    # 5 HARNESS messages — should be EXCLUDED.
    for variant in range(5):
        v = _cluster_at(1, dim=8, seed=100 + variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"harness-{variant}",
            project_slug="proj-A",
            content=f"<ide_opened_file>file{variant}.md</ide_opened_file>",
            vector=v,
            content_kind="HARNESS",
        )
        next_id += 1
    # 5 PROCEDURAL messages — should be EXCLUDED.
    for variant in range(5):
        v = _cluster_at(2, dim=8, seed=200 + variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"proc-{variant}",
            project_slug="proj-A",
            content=f"Let me check the patterns {variant}",
            vector=v,
            role="assistant",
            content_kind="PROCEDURAL",
        )
        next_id += 1
    # 5 TOOL_RESULT_EMBEDDED messages — should be EXCLUDED.
    for variant in range(5):
        v = _cluster_at(3, dim=8, seed=300 + variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"tool-{variant}",
            project_slug="proj-A",
            content=f"public class Foo{variant} {{ }}",
            vector=v,
            role="assistant",
            content_kind="TOOL_RESULT_EMBEDDED",
        )
        next_id += 1

    response = topics.run_topics(
        db_conn, similarity_threshold=0.5, min_cluster_size=3,
    )

    # Only the 5 THOUGHT-kind messages clustered; the other 15 are
    # filtered out before clustering even runs.
    assert response.total_messages_clustered == 5
    assert response.total_clusters == 1
    assert response.themes[0].cluster_size == 5


def test_topics_includes_null_content_kind_rows(db_conn):
    """Rows with NULL content_kind (interrupted or skipped migration)
    are included alongside THOUGHT — same NULL-tolerance discipline as
    the timestamp filter. Avoids silently dropping otherwise-valid data."""
    next_id = 1
    for variant in range(5):
        v = _cluster_at(0, dim=8, seed=variant)
        _seed_message(
            db_conn,
            msg_id=next_id,
            session_id=f"null-{variant}",
            project_slug="proj-A",
            content=f"unclassified message {variant}",
            vector=v,
            content_kind="THOUGHT",
        )
        next_id += 1
    # Patch one row to NULL content_kind to simulate a partially-migrated DB.
    db_conn.execute("UPDATE messages SET content_kind = NULL WHERE msg_id = 1")
    db_conn.commit()

    response = topics.run_topics(
        db_conn, similarity_threshold=0.5, min_cluster_size=3,
    )

    # All 5 still clustered — NULL kind treated as eligible.
    assert response.total_messages_clustered == 5
