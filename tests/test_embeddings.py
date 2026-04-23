"""Tests for claude_recall.embeddings (v0.3).

Unit tests use a hand-rolled httpx transport so no network is touched. A live
smoke test runs only when OLLAMA_LIVE=1 is set in the env so CI stays stable.
"""

from __future__ import annotations

import json
import os

import httpx
import numpy as np
import pytest

from claude_recall import embeddings

# ---------- pack / unpack ----------

def test_pack_unpack_roundtrip():
    rng = np.random.default_rng(42)
    v = rng.standard_normal(768).astype(np.float32)
    blob = embeddings.pack_vector(v)
    assert len(blob) == 768 * 4
    restored = embeddings.unpack_vector(blob, 768)
    np.testing.assert_array_equal(v, restored)


def test_pack_vector_rejects_non_1d():
    with pytest.raises(ValueError):
        embeddings.pack_vector(np.zeros((2, 3), dtype=np.float32))


def test_unpack_vector_rejects_wrong_length():
    with pytest.raises(ValueError):
        embeddings.unpack_vector(b"\x00" * 100, dim=768)


def test_pack_is_little_endian_float32():
    v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    blob = embeddings.pack_vector(v)
    # 1.0 as float32 LE bytes
    assert blob[:4] == b"\x00\x00\x80\x3f"


# ---------- cosine_matrix ----------

def test_cosine_matrix_identical_returns_one():
    v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    mat = np.stack([v, v, v])
    result = embeddings.cosine_matrix(v, mat)
    np.testing.assert_allclose(result, [1.0, 1.0, 1.0], atol=1e-6)


def test_cosine_matrix_orthogonal_returns_zero():
    q = np.array([1.0, 0.0], dtype=np.float32)
    c = np.array([[0.0, 1.0], [0.0, -1.0]], dtype=np.float32)
    result = embeddings.cosine_matrix(q, c)
    np.testing.assert_allclose(result, [0.0, 0.0], atol=1e-6)


def test_cosine_matrix_handles_zero_query():
    q = np.zeros(3, dtype=np.float32)
    c = np.ones((4, 3), dtype=np.float32)
    result = embeddings.cosine_matrix(q, c)
    assert result.shape == (4,)
    assert (result == 0.0).all()


def test_cosine_matrix_handles_zero_candidate_row():
    q = np.array([1.0, 0.0], dtype=np.float32)
    c = np.array([[0.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    result = embeddings.cosine_matrix(q, c)
    # Zero candidate → 0.0, unit candidate aligned with query → 1.0
    np.testing.assert_allclose(result, [0.0, 1.0], atol=1e-6)


def test_cosine_matrix_rejects_shape_mismatch():
    q = np.zeros(3, dtype=np.float32)
    c = np.zeros((2, 4), dtype=np.float32)
    with pytest.raises(ValueError):
        embeddings.cosine_matrix(q, c)


def test_cosine_matrix_ranks_closer_first():
    """Synthetic numerical check of rerank behavior."""
    q = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    closer = np.array([0.9, 0.1, 0.0], dtype=np.float32)
    further = np.array([0.5, 0.5, 0.0], dtype=np.float32)
    scores = embeddings.cosine_matrix(q, np.stack([further, closer]))
    # closer was second in the stack; its score should be higher
    assert scores[1] > scores[0]


# ---------- OllamaClient (httpx mocked) ----------

def _mock_client(handler) -> embeddings.OllamaClient:
    """Build an OllamaClient whose inner httpx.Client uses a MockTransport."""
    transport = httpx.MockTransport(handler)
    client = embeddings.OllamaClient()
    client._client.close()
    client._client = httpx.Client(transport=transport, timeout=5.0)
    return client


def test_embed_single_returns_768_dim():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        body = json.loads(request.content)
        assert body["model"] == "nomic-embed-text"
        assert body["input"] == ["hello"]
        return httpx.Response(
            200,
            json={"embeddings": [[0.1] * 768]},
        )

    with _mock_client(handler) as c:
        vec = c.embed("hello")
    assert vec.shape == (768,)
    assert vec.dtype == np.float32


def test_embed_batch_preserves_order():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Echo a distinguishable vector per text so we can verify order.
        base = [float(i) for i in range(768)]
        rows = []
        for idx, _ in enumerate(body["input"]):
            rows.append([v + idx for v in base])
        return httpx.Response(200, json={"embeddings": rows})

    with _mock_client(handler) as c:
        mat = c.embed_batch(["a", "b", "c"])
    assert mat.shape == (3, 768)
    # Row i should equal base + i
    assert mat[0, 0] == 0.0
    assert mat[1, 0] == 1.0
    assert mat[2, 0] == 2.0


def test_embed_batch_empty_input_short_circuits():
    def handler(request):  # pragma: no cover - should not be called
        raise AssertionError("empty input should not hit the network")

    with _mock_client(handler) as c:
        mat = c.embed_batch([])
    assert mat.shape == (0, embeddings.DEFAULT_DIM)


def test_embed_raises_on_http_error():
    def handler(request):
        return httpx.Response(500, text="kaboom")

    with _mock_client(handler) as c:
        with pytest.raises(embeddings.EmbeddingError):
            c.embed("text")


def test_embed_raises_on_connection_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with _mock_client(handler) as c:
        with pytest.raises(embeddings.EmbeddingError):
            c.embed("text")


def test_embed_raises_on_shape_mismatch():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [[0.1] * 768]})

    with _mock_client(handler) as c:
        with pytest.raises(embeddings.EmbeddingError):
            c.embed_batch(["a", "b"])  # requested 2, got 1


def test_embed_raises_on_non_finite():
    def handler(request):
        return httpx.Response(
            200, json={"embeddings": [[float("nan")] + [0.0] * 767]}
        )

    with _mock_client(handler) as c:
        with pytest.raises(embeddings.EmbeddingError):
            c.embed("text")


def test_embed_raises_on_zero_norm():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [[0.0] * 768]})

    with _mock_client(handler) as c:
        with pytest.raises(embeddings.EmbeddingError):
            c.embed("text")


# ---------- OllamaClient.probe ----------

def test_probe_ollama_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with _mock_client(handler) as c:
        result = c.probe()
    assert result.ollama_reachable is False
    assert result.model_present is False
    assert result.embed_ok is False
    assert result.error is not None


def test_probe_model_not_pulled():
    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.18.2"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llava:latest"}]})
        raise AssertionError(f"unexpected path {request.url.path}")

    with _mock_client(handler) as c:
        result = c.probe()
    assert result.ollama_reachable is True
    assert result.version == "0.18.2"
    assert result.model_present is False
    assert result.embed_ok is False
    assert "pull" in (result.error or "")


def test_probe_happy_path():
    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.18.2"})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "nomic-embed-text:latest"}]}
            )
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1] * 768]})
        raise AssertionError(f"unexpected path {request.url.path}")

    with _mock_client(handler) as c:
        result = c.probe()
    assert result.ollama_reachable is True
    assert result.model_present is True
    assert result.embed_ok is True
    assert result.dim == 768
    assert result.error is None


# ---------- live smoke test ----------

@pytest.mark.skipif(
    os.environ.get("OLLAMA_LIVE") != "1",
    reason="live Ollama test — set OLLAMA_LIVE=1 to enable",
)
def test_live_embed_against_real_ollama():
    """End-to-end check against a real localhost Ollama. Skipped in CI."""
    with embeddings.OllamaClient() as c:
        vec = c.embed("the quick brown fox")
    assert vec.shape == (768,)
    assert np.linalg.norm(vec) > 0
