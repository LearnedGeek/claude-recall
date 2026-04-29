"""Hybrid retrieval tests — FTS5 candidate pool + semantic rerank.

Uses a fake OllamaClient (see tests/test_cli.py::_FakeOllama) so no network
is touched. The core claim under test: semantic rerank flips the order of
BM25 candidates when an off-topic message scores high on keyword overlap
but low on conceptual similarity, and vice versa.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from claude_recall import embeddings, indexer, search, storage


@pytest.fixture
def tiny_indexed_db(tmp_path):
    """DB with three short user messages whose BM25 and semantic order can diverge."""
    archive = tmp_path / "archive"
    (archive / "proj").mkdir(parents=True)
    jsonl = archive / "proj" / "session_rerank.jsonl"
    # All three contain "cascade" once so FTS5 ranks them roughly equal by
    # term-frequency; semantic rerank differentiates them.
    jsonl.write_text(
        "\n".join([
            '{"type":"user","message":{"role":"user",'
            '"content":"cascade feedback-loop architecture"},'
            '"timestamp":"2026-04-20T10:00:00Z"}',
            '{"type":"user","message":{"role":"user",'
            '"content":"cascade of car problems in the parking lot"},'
            '"timestamp":"2026-04-20T10:01:00Z"}',
            '{"type":"user","message":{"role":"user",'
            '"content":"cascade styling concerns for the UI"},'
            '"timestamp":"2026-04-20T10:02:00Z"}',
        ]),
        encoding="utf-8",
    )
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    indexer.run_index(conn, archive)
    yield conn
    conn.close()


def _seed_vectors(conn, assignments: dict[int, np.ndarray], *, model: str = "m", dim: int = 4):
    """Insert fake vectors for specific msg_ids."""
    now = datetime.now(UTC).isoformat()
    for mid, v in assignments.items():
        conn.execute(
            "INSERT OR REPLACE INTO message_vectors"
            "(msg_id, vector, model, dim, embedded_at) VALUES (?,?,?,?,?)",
            (mid, embeddings.pack_vector(v.astype(np.float32)), model, dim, now),
        )
    conn.commit()


class _ScriptedClient:
    """Returns pre-assigned query vectors. No network."""

    def __init__(self, query_vec: np.ndarray):
        self.query_vec = query_vec.astype(np.float32)
        self.embed_calls = 0

    def embed(self, text):
        self.embed_calls += 1
        return self.query_vec

    def embed_batch(self, texts):
        return np.stack([self.query_vec] * len(texts))

    def close(self):
        pass


def test_semantic_rerank_flips_order(tiny_indexed_db):
    """When semantic vectors disagree with BM25, rerank returns the semantic winner first."""
    # All three messages match "cascade" in FTS5; BM25 order will be
    # file-order-ish (msg_ids 1, 2, 3 corresponding to our three inputs).
    # Assign semantic vectors so msg 1 (architecture) is closest to the query.
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    _seed_vectors(
        tiny_indexed_db,
        {
            1: np.array([0.95, 0.05, 0.0, 0.0], dtype=np.float32),   # closest
            2: np.array([0.10, 0.99, 0.0, 0.0], dtype=np.float32),   # far
            3: np.array([0.50, 0.50, 0.0, 0.0], dtype=np.float32),   # middle
        },
    )
    client = _ScriptedClient(query_vec)

    resp = search.run_search(
        tiny_indexed_db,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=3,
    )
    assert resp.semantic_used is True
    assert resp.semantic_fallback_reason is None
    assert client.embed_calls == 1
    contents = [r.content_preview for r in resp.results]
    assert "architecture" in contents[0].lower()
    # Ranks populated
    assert all(r.semantic_rank is not None for r in resp.results)
    assert all(r.bm25_rank is not None for r in resp.results)


def test_semantic_fallback_when_no_vectors(tiny_indexed_db):
    """With no vectors in the index, semantic=True falls back to FTS5 + reason."""
    client = _ScriptedClient(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    resp = search.run_search(
        tiny_indexed_db,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=3,
    )
    assert resp.semantic_used is False
    assert resp.semantic_fallback_reason is not None
    assert "vectors" in resp.semantic_fallback_reason
    assert client.embed_calls == 0


def test_semantic_fallback_when_ollama_fails(tiny_indexed_db):
    """If the query embed raises, we fall back to FTS5 order with a reason."""
    # Seed vectors so the "no vectors" path doesn't short-circuit first.
    _seed_vectors(
        tiny_indexed_db,
        {
            1: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            2: np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
            3: np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32),
        },
    )

    class _FailingClient:
        def embed(self, text):
            raise embeddings.EmbeddingError("simulated outage")

        def close(self):
            pass

    resp = search.run_search(
        tiny_indexed_db,
        "cascade",
        semantic=True,
        ollama_client=_FailingClient(),
        limit=3,
    )
    assert resp.semantic_used is False
    assert "embed failed" in (resp.semantic_fallback_reason or "").lower()
    # Still returns FTS5 results, not an empty response
    assert len(resp.results) == 3


def test_semantic_off_preserves_v02_behavior(tiny_indexed_db):
    """semantic=False (default) must not call Ollama and must not populate ranks."""
    client = _ScriptedClient(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    resp = search.run_search(tiny_indexed_db, "cascade", limit=3)
    assert resp.semantic_used is False
    assert client.embed_calls == 0
    assert all(r.bm25_rank is None for r in resp.results)
    assert all(r.semantic_rank is None for r in resp.results)


def test_semantic_partial_vector_coverage(tiny_indexed_db):
    """Messages without vectors keep their BM25 rank but are pushed below vectored ones."""
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    # Only msg 2 has a vector, and it's close to query.
    _seed_vectors(
        tiny_indexed_db,
        {2: np.array([0.9, 0.1, 0.0, 0.0], dtype=np.float32)},
    )
    client = _ScriptedClient(query_vec)
    resp = search.run_search(
        tiny_indexed_db,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=3,
    )
    assert resp.semantic_used is True
    # msg 2 (vectorless msg_ids get -inf) should lead
    assert "car problems" in resp.results[0].content_preview.lower()


@pytest.fixture
def multi_project_indexed_db(tmp_path):
    """DB with 5 messages spread across 3 projects, all matching 'cascade'.

    proj-A: 3 messages
    proj-B: 1 message
    proj-C: 1 message

    Returns a (conn, msg_ids_by_project) tuple so tests can address each
    message by project + index. msg_ids depend on the indexer's
    file-walk order, so we look them up dynamically rather than hardcoding.
    """
    archive = tmp_path / "archive"
    for proj, count in [("proj-A", 3), ("proj-B", 1), ("proj-C", 1)]:
        proj_dir = archive / proj
        proj_dir.mkdir(parents=True)
        # File names must be unique because the indexer derives session_id
        # from the file stem in the flat layout — same stem across projects
        # would collide on the session_id PK.
        jsonl = proj_dir / f"session-{proj}.jsonl"
        lines = []
        for i in range(count):
            content = f"cascade variant {proj} {i}"
            ts = f"2026-04-20T10:0{i}:00Z"
            lines.append(
                '{"type":"user","message":{"role":"user","content":"'
                + content + '"},"timestamp":"' + ts + '"}'
            )
        jsonl.write_text("\n".join(lines), encoding="utf-8")
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    indexer.run_index(conn, archive)

    # Resolve msg_ids by project. Within a project, order by msg_id so the
    # caller can address them as ids[proj][0], ids[proj][1], etc.
    ids_by_project: dict[str, list[int]] = {}
    rows = conn.execute(
        "SELECT m.msg_id, s.project_slug FROM messages m "
        "JOIN sessions s ON s.session_id = m.session_id "
        "ORDER BY s.project_slug, m.msg_id"
    ).fetchall()
    for row in rows:
        ids_by_project.setdefault(row["project_slug"], []).append(int(row["msg_id"]))

    yield conn, ids_by_project
    conn.close()


def test_cross_project_boost_promotes_recurring_themes(multi_project_indexed_db):
    """5 candidates with cosine in a tight band. proj-A contributes 3 hits,
    proj-B and proj-C contribute 1 each. Without boost, the highest single
    cosine wins. With boost, a proj-A hit should rank ahead of proj-B/C
    once the 1.05× / 1.10× multiplier kicks in.

    Cosine assignments are deliberately staggered so the no-boost order
    has proj-B's hit at rank 1 (cosine 0.92), and proj-A's best hit at
    rank 2 (cosine 0.90). With the 1.10× boost (3-hit project), proj-A's
    score becomes 0.99 — clearly above proj-B's 0.92 — and it takes rank 1.
    """
    conn, ids = multi_project_indexed_db
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    _seed_vectors(
        conn,
        {
            ids["proj-A"][0]: np.array([0.90, 0.44, 0.0, 0.0], dtype=np.float32),
            ids["proj-A"][1]: np.array([0.85, 0.53, 0.0, 0.0], dtype=np.float32),
            ids["proj-A"][2]: np.array([0.80, 0.60, 0.0, 0.0], dtype=np.float32),
            ids["proj-B"][0]: np.array([0.92, 0.39, 0.0, 0.0], dtype=np.float32),
            ids["proj-C"][0]: np.array([0.83, 0.56, 0.0, 0.0], dtype=np.float32),
        },
    )
    client = _ScriptedClient(query_vec)

    # Without boost: proj-B's 0.92 wins.
    no_boost = search.run_search(
        conn,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=5,
    )
    assert no_boost.semantic_used is True
    assert no_boost.results[0].project_slug == "proj-B"

    # With boost: proj-A (3-hit project) gets 1.10× → 0.99 > 0.92 (proj-B).
    boosted = search.run_search(
        conn,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=5,
        cross_project_boost=True,
    )
    assert boosted.semantic_used is True
    assert boosted.results[0].project_slug == "proj-A", (
        f"cross-project boost did not promote proj-A's recurring hits: "
        f"top result was {boosted.results[0].project_slug!r}"
    )


def test_cross_project_boost_capped_does_not_overpower_clear_winner(
    multi_project_indexed_db,
):
    """A clearly higher-cosine single-project hit must not be displaced by
    the multiplicative boost. proj-A has 3 middling hits; proj-B has 1
    obviously closer hit. Even at the boost cap (1.5×), proj-A's best
    score (0.60 × 1.10 = 0.66) is well below proj-B's (0.99 raw).
    """
    conn, ids = multi_project_indexed_db
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    _seed_vectors(
        conn,
        {
            ids["proj-A"][0]: np.array([0.60, 0.80, 0.0, 0.0], dtype=np.float32),
            ids["proj-A"][1]: np.array([0.55, 0.84, 0.0, 0.0], dtype=np.float32),
            ids["proj-A"][2]: np.array([0.50, 0.87, 0.0, 0.0], dtype=np.float32),
            ids["proj-B"][0]: np.array([0.99, 0.14, 0.0, 0.0], dtype=np.float32),
            ids["proj-C"][0]: np.array([0.30, 0.95, 0.0, 0.0], dtype=np.float32),
        },
    )
    client = _ScriptedClient(query_vec)

    boosted = search.run_search(
        conn,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=5,
        cross_project_boost=True,
    )
    assert boosted.results[0].project_slug == "proj-B", (
        f"boost overpowered a clear higher-cosine winner: top was "
        f"{boosted.results[0].project_slug!r} at semantic_rank=0 "
        f"(should be proj-B's 0.99 cosine)"
    )


def test_cross_project_boost_silently_noop_with_project_filter(
    multi_project_indexed_db,
):
    """When --project scopes the pool to one project, boost has nothing
    to promote and must produce identical output to the no-boost case.
    No warning, no error — same shape as how --semantic silently degrades."""
    conn, ids = multi_project_indexed_db
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    _seed_vectors(
        conn,
        {
            ids["proj-A"][0]: np.array([0.90, 0.44, 0.0, 0.0], dtype=np.float32),
            ids["proj-A"][1]: np.array([0.85, 0.53, 0.0, 0.0], dtype=np.float32),
            ids["proj-A"][2]: np.array([0.80, 0.60, 0.0, 0.0], dtype=np.float32),
        },
    )
    client = _ScriptedClient(query_vec)

    no_boost = search.run_search(
        conn,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=5,
        project_slug="proj-A",
    )
    boosted = search.run_search(
        conn,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=5,
        project_slug="proj-A",
        cross_project_boost=True,
    )
    no_boost_order = [r.turn_index for r in no_boost.results]
    boosted_order = [r.turn_index for r in boosted.results]
    assert no_boost_order == boosted_order, (
        f"boost should silently no-op when scoped to one project, but "
        f"order differs: no-boost={no_boost_order!r} vs boosted={boosted_order!r}"
    )


def test_rerank_pool_size_caps_candidates(tiny_indexed_db):
    """rerank_pool_size limits the FTS5 fetch to the pool; limit further caps output."""
    query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    _seed_vectors(
        tiny_indexed_db,
        {
            1: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            2: np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
            3: np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32),
        },
    )
    client = _ScriptedClient(query_vec)
    resp = search.run_search(
        tiny_indexed_db,
        "cascade",
        semantic=True,
        ollama_client=client,
        limit=1,
        rerank_pool_size=2,
    )
    # Pool was 2, so we should have at most 2 candidates considered; limit=1 returns 1.
    assert len(resp.results) == 1
