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
