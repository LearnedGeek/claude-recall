"""Ollama embedding client + vector ops (v0.3).

Part of the optional ``[embeddings]`` extra. Requires ``numpy`` and ``httpx``.
Core ``claude-recall`` stays zero-dep; importing this module fails cleanly if
the extra isn't installed, letting callers guard with a try/except.

See docs/EMBEDDINGS-PLAN.md for the full design, and docs/PLAN.md §4 + §8 for
the envelope (model, config block, opt-in framing).
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import httpx
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised at install time
    raise ImportError(
        "claude-recall embeddings layer requires the [embeddings] extra. "
        "Install with: pip install 'claude-recall[embeddings]'"
    ) from exc

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_TIMEOUT = 10.0
DEFAULT_DIM = 768


class EmbeddingError(RuntimeError):
    """Raised for any terminal embedding-path failure.

    Callers that need to degrade gracefully (CLI search, hook path) should
    catch this and fall back to FTS5-only results.
    """


@dataclass
class ProbeResult:
    ollama_reachable: bool
    version: str | None
    model_present: bool
    embed_ok: bool
    dim: int | None
    error: str | None = None


class OllamaClient:
    """Thin synchronous httpx wrapper around Ollama's /api/embed endpoint.

    One instance per CLI invocation is enough. No connection pooling beyond
    httpx defaults; the embed payload size is small and the call count is
    bounded by ``rerank_pool_size`` for searches or corpus size for
    ``claude-recall embed``.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def embed(self, text: str) -> np.ndarray:
        """Embed a single string. Returns a float32 ndarray of shape (dim,)."""
        matrix = self.embed_batch([text])
        return matrix[0]

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """Embed a list of strings.

        Returns a float32 ndarray of shape (len(texts), dim). Preserves input
        order. Raises EmbeddingError on any HTTP or shape failure; validates
        that every returned vector is finite and has non-zero norm.
        """
        if not texts:
            return np.zeros((0, DEFAULT_DIM), dtype=np.float32)
        try:
            resp = self._client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Ollama request failed: {exc}") from exc
        except ValueError as exc:
            raise EmbeddingError(f"Ollama returned non-JSON: {exc}") from exc

        raw = data.get("embeddings")
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise EmbeddingError(
                f"Ollama returned {len(raw) if isinstance(raw, list) else '?'} "
                f"embeddings for {len(texts)} inputs"
            )

        matrix = np.asarray(raw, dtype=np.float32)
        if matrix.ndim != 2:
            raise EmbeddingError(
                f"Unexpected embedding shape: {matrix.shape}"
            )
        if not np.isfinite(matrix).all():
            raise EmbeddingError("Ollama returned non-finite values")
        norms = np.linalg.norm(matrix, axis=1)
        if (norms == 0).any():
            raise EmbeddingError("Ollama returned zero-norm vector(s)")
        return matrix

    def probe(self) -> ProbeResult:
        """Diagnose the full embedding path without raising.

        Returns a ProbeResult with per-step booleans so the CLI can tell the
        user exactly which step failed. Never raises.
        """
        version: str | None = None
        try:
            v = self._client.get(f"{self.base_url}/api/version")
            v.raise_for_status()
            version = v.json().get("version")
        except httpx.HTTPError as exc:
            return ProbeResult(
                ollama_reachable=False,
                version=None,
                model_present=False,
                embed_ok=False,
                dim=None,
                error=f"cannot reach Ollama at {self.base_url}: {exc}",
            )

        try:
            t = self._client.get(f"{self.base_url}/api/tags")
            t.raise_for_status()
            tags = t.json().get("models", [])
        except httpx.HTTPError as exc:
            return ProbeResult(
                ollama_reachable=True,
                version=version,
                model_present=False,
                embed_ok=False,
                dim=None,
                error=f"cannot list Ollama models: {exc}",
            )
        names = {m.get("name", "").split(":")[0] for m in tags}
        model_name = self.model.split(":")[0]
        if model_name not in names:
            return ProbeResult(
                ollama_reachable=True,
                version=version,
                model_present=False,
                embed_ok=False,
                dim=None,
                error=(
                    f"model {self.model!r} not pulled. "
                    f"Run: ollama pull {self.model}"
                ),
            )

        try:
            vec = self.embed("probe")
        except EmbeddingError as exc:
            return ProbeResult(
                ollama_reachable=True,
                version=version,
                model_present=True,
                embed_ok=False,
                dim=None,
                error=f"embed probe failed: {exc}",
            )

        return ProbeResult(
            ollama_reachable=True,
            version=version,
            model_present=True,
            embed_ok=True,
            dim=int(vec.shape[0]),
            error=None,
        )


# --- vector BLOB + cosine ---------------------------------------------------

def pack_vector(v: np.ndarray) -> bytes:
    """Serialize a float vector to little-endian float32 bytes for SQLite BLOB.

    Deterministic: identical vectors produce identical bytes across platforms.
    """
    arr = np.asarray(v, dtype="<f4")
    if arr.ndim != 1:
        raise ValueError(f"pack_vector expects a 1-D array, got {arr.shape}")
    return arr.tobytes()


def unpack_vector(blob: bytes, dim: int) -> np.ndarray:
    """Deserialize a BLOB produced by pack_vector back into a float32 array."""
    expected = dim * 4
    if len(blob) != expected:
        raise ValueError(
            f"blob length {len(blob)} does not match expected {expected} "
            f"for dim={dim}"
        )
    return np.frombuffer(blob, dtype="<f4").copy()  # copy so it's writable


def cosine_matrix(query: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    """Cosine similarity between a single query and a matrix of candidates.

    ``query``: shape (D,). ``candidates``: shape (N, D). Returns shape (N,)
    with values in roughly [-1, 1]. Handles zero-norm defensively (rare after
    OllamaClient validation, but cheap).
    """
    if query.ndim != 1:
        raise ValueError(f"query must be 1-D, got {query.shape}")
    if candidates.ndim != 2 or candidates.shape[1] != query.shape[0]:
        raise ValueError(
            f"candidates shape {candidates.shape} incompatible with "
            f"query shape {query.shape}"
        )
    qn = float(np.linalg.norm(query))
    if qn == 0.0:
        return np.zeros(candidates.shape[0], dtype=np.float32)
    cn = np.linalg.norm(candidates, axis=1)
    safe_cn = np.where(cn == 0.0, 1.0, cn)
    dots = candidates @ query
    return (dots / (safe_cn * qn)).astype(np.float32)
