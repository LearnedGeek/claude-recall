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
                  turn_index=0):
    """Insert a session row (if needed), a messages row, and a message_vectors
    row with the given vector. Self-contained — no fixture archive required.
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
        "timestamp, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, session_id, role, content, turn_index, started_at, ""),
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
