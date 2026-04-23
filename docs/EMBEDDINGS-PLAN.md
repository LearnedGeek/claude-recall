# claude-recall — v0.3 Embeddings Layer Plan

**Audience:** The agent or engineer implementing v0.3.
**Status:** Design complete. Scoping approved 2026-04-23. Implementation has not started.
**Relation to [docs/PLAN.md](PLAN.md):** PLAN is the overall product spec. This document scopes the single v0.3 feature. Where PLAN already commits us (tech stack, retrieval shape, opt-in framing) this doc defers; where PLAN left the call open, this doc fills it in.

---

## 1. Intent

Add **semantic retrieval** to claude-recall via Ollama-hosted `nomic-embed-text` embeddings, layered behind the existing FTS5 ranking as a **pure rerank** of the top-50 BM25 candidates. Catch the cases FTS5 misses — prompts that reference a concept under different wording than the archived messages used ("the cascade" → "feedback loop").

**Load-bearing constraint:** Core install stays zero-dep. Embeddings ride on the optional `[embeddings]` pip extra (numpy + httpx), and even when installed the feature is off by default (`[embeddings].enabled=false`).

---

## 2. Non-goals

- **Not a vector database.** SQLite stores vectors as BLOBs. No FAISS, no Qdrant, no Chroma.
- **Not a replacement for FTS5.** Hybrid: FTS5 produces the candidate pool, embeddings rerank it. Pure-semantic search is not a v0.3 mode.
- **Not chunking.** Full-message embeddings only. Long-message chunking is a v0.3.1+ consideration.
- **Not a remote embedding service.** Local Ollama only. No OpenAI/Cohere/HF-Inference.
- **Not telemetry.** No opt-in usage reporting. Same stance as MVP (PLAN §17 D4).
- **Not multi-model.** `nomic-embed-text` exclusively for v0.3. Model pluggability is v0.3.1+.
- **Not query-time embedding of long prompts.** Prompts are typically < 1KB; one embed call per query is sufficient.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────┐
│  claude_recall.indexer (existing)                     │
│  writes messages row, triggers FTS5 sync              │
└───────────────────────┬──────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────┐
│  claude_recall.embeddings  (NEW)                      │
│  - OllamaClient.embed(text) → np.float32[768]          │
│  - OllamaClient.embed_batch(texts) → np.float32[N,768] │
│  - cosine(a, b), cosine_matrix(q, M)                   │
│  - pack_vector(v) / unpack_vector(blob)                │
└───────────────────────┬──────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────┐
│  SQLite: message_vectors table (NEW)                  │
│  (msg_id, vector BLOB, model, dim, embedded_at)       │
└───────────────────────┬──────────────────────────────┘
                        │ queried by
                        ▼
┌──────────────────────────────────────────────────────┐
│  claude_recall.search (existing, extended)            │
│  run_search(..., semantic=False)                       │
│   └─ when semantic=True:                              │
│        1. Run existing FTS5 query, LIMIT 50           │
│        2. Embed the query string → q                  │
│        3. Load 50 candidate vectors, cosine-rank      │
│        4. Return top --limit results                  │
└──────────────────────────────────────────────────────┘
```

The existing retrieval path is unchanged when `semantic=False`. The new path is a strict superset: FTS5 runs first, then rerank runs on its output. No path exists where FTS5 is skipped.

---

## 4. Tech stack additions

| Component | Choice | Rationale |
|---|---|---|
| Embedding model | `nomic-embed-text` via Ollama | Already locked by PLAN §4. 768-dim, runs locally, widely pre-pulled on Ollama users' machines. |
| HTTP client | `httpx` (already named in PLAN §4 `[embeddings]` extra) | Synchronous API is enough; async not needed at MVP rerank scale. |
| Numeric kernel | `numpy` (already named in PLAN §4) | `float32` BLOB pack, vectorized cosine. |
| Storage | SQLite BLOB column on a dedicated table (see §5) | One file backup story preserved. |
| Packaging | `[embeddings]` pip extra | Core install stays zero-dep. |

No other new dependencies.

---

## 5. Data model additions

### 5.1 New table

```sql
CREATE TABLE IF NOT EXISTS message_vectors (
    msg_id       INTEGER PRIMARY KEY,
    vector       BLOB    NOT NULL,   -- np.float32 little-endian, 768 × 4 = 3072 bytes
    model        TEXT    NOT NULL,   -- 'nomic-embed-text'
    dim          INTEGER NOT NULL,   -- 768 for nomic-embed-text
    embedded_at  TEXT    NOT NULL,   -- ISO8601, for incremental re-embed
    FOREIGN KEY (msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_message_vectors_model
    ON message_vectors(model, dim);
```

Added unconditionally to `SCHEMA_DDL`. Users who never run `claude-recall embed` pay the cost of one empty table — negligible.

### 5.2 Schema version

**No schema-version bump.** The table is additive and unused until `embed` runs. Existing v1 databases get the table via `CREATE TABLE IF NOT EXISTS` on next `open_db()`. No migration is needed.

### 5.3 BLOB format

Vectors are packed as contiguous little-endian float32 bytes via `np.asarray(v, dtype='<f4').tobytes()`. Deterministic, zero-copy load via `np.frombuffer(blob, dtype='<f4')`. Model + dim stored alongside so a future model swap can invalidate stale rows.

### 5.4 ON DELETE CASCADE

Re-indexing a changed session deletes its `messages` rows, which cascades to `message_vectors`. Embeddings for touched sessions are lost on re-index and must be re-embedded — matches the "file changed → session re-parsed" semantics already established in PLAN §5.2.

---

## 6. CLI additions

### 6.1 `claude-recall embed`

**Purpose:** Compute embeddings for messages that don't yet have one.

**Usage:**
```
claude-recall embed [--project <slug>] [--rebuild] [--batch-size N] [--probe] [--verbose]
```

**Options:**
- `--project <slug>` — limit to one project (also accepts `auto`, same as `search`).
- `--rebuild` — drop all vectors in scope and re-embed from scratch.
- `--batch-size N` — override `[embeddings].batch_size` (default 32). Ollama handles up to ~50 texts per call without degradation.
- `--probe` — instead of embedding, test the Ollama path end-to-end: connect, list models, verify `nomic-embed-text` is pulled, embed a 5-word probe string. Exit 0 on success; prints the failed step on exit 1.
- `--verbose` — per-batch progress to stderr.

**Behavior:**
1. Guard: if `[embeddings].enabled=false`, exit 1 with a pointer to enable it in config.
2. Connect to Ollama; on failure, exit 2 with the specific reason.
3. Query `SELECT m.msg_id, m.content FROM messages m LEFT JOIN message_vectors v ON v.msg_id = m.msg_id WHERE v.msg_id IS NULL` (adding project scope if requested).
4. Batch-embed `content` values, insert rows into `message_vectors` with `model`, `dim`, `embedded_at`.
5. Print summary: `embedded N messages in T seconds (M/s)`.

**Exit codes:** 0 success; 1 embeddings disabled; 2 Ollama error; 3 DB error.

**Performance target:** ANI's 25k messages in under 5 minutes on consumer hardware (requires batch_size ≥ 32 + Ollama keep_alive set).

### 6.2 `claude-recall search --semantic`

Existing `search` command gains `--semantic`. When set:

1. FTS5 runs as today (same `--extract-keywords`, `--from-config`, `--project auto` semantics) with an internal pool size override of `rerank_pool_size` (default 50). The user-facing `--limit` still caps the final output.
2. Embed the query string (after keyword-extraction if `--extract-keywords` is also passed — the embedding is computed from the extracted keywords string, not the raw prompt).
3. Load vectors for the pool. Rows whose `message_vectors` row is missing score 0 (they remain at their BM25 rank but won't win the rerank; acceptable tradeoff vs. synchronously embedding them mid-query).
4. Cosine-rank the pool. Return top `--limit`.
5. Each `SearchResult` gains two fields: `bm25_rank`, `semantic_rank` (ints, `None` when semantic=False). Helpful for debuggability and for a future rerank-visualization CLI.

**Soft-ignore on disabled:** If `--semantic` is passed but `[embeddings].enabled=false`, log a single-line warning to stderr and run FTS5-only. Don't error. This matters for the hook (see §7).

**Failure mode:** On any Ollama error during `--semantic` (query embed fails, timeout, HTTP 5xx), warn to stderr and fall back to pure FTS5 output.

### 6.3 `claude-recall status` additions

New JSON fields:
- `embeddings_enabled: bool` (from config)
- `ollama_reachable: bool` (probes the `/api/version` endpoint, 2s timeout)
- `vectors_indexed: int` (`COUNT(*)` on `message_vectors`)
- `messages_without_vectors: int` (for `claude-recall embed` sizing)

New check in `checks`:
- `embeddings_ready: bool` = `embeddings_enabled AND ollama_reachable AND vectors_indexed > 0`

The `agent-context` one-line output gains a suffix when embeddings are enabled and healthy:
```
claude-recall: 87 sessions, 12,402 messages indexed, most recent 2026-04-22T20:47. Embeddings: 12,402 vectors, Ollama reachable.
```

When unhealthy, the suffix tells the user what's broken:
```
... Embeddings: disabled / Ollama unreachable / 0 vectors (run `claude-recall embed`).
```

### 6.4 `claude-recall init-hooks` — unchanged surface

Hook scripts shipped with v0.3 pass `--semantic` unconditionally. The soft-ignore in §6.2 means it's a no-op when embeddings aren't enabled. Users enabling embeddings after `init-hooks` don't need to re-run it.

---

## 7. Hook changes

`on_prompt.sh`:

```bash
RESULT=$(claude-recall search "$PROMPT" \
    --project auto --from-config --extract-keywords \
    --semantic --agent-context 2>/dev/null)
```

`on_prompt.ps1` mirror.

**Budget analysis** (PLAN §7.3 says ≤ 500ms):
- Python + CLI startup: ~150ms
- FTS5 keyword-extracted query: ~10ms
- Ollama embed (prompt only, warm model, localhost): ~30-60ms
- Rerank (50 × 768 cosine): < 5ms
- Agent-context format: < 5ms
- **Total: ~200-230ms on warm path. Comfortable under budget.**

**Cold-Ollama path** (model not loaded, first prompt after Ollama start): 1-3s for model load. This is outside budget, but the hook's existing failsafe (`--semantic` soft-ignores or Ollama-unreachable degrades to FTS5) keeps it under budget by falling back. First-prompt-after-Ollama-restart quality regression is acceptable — subsequent prompts hit warm Ollama and get the full semantic path.

**New failsafe path:** if `httpx` timeout hits (default 10s via config), the embedding call fails, the CLI logs a warning to stderr, FTS5-only results are returned. Hook still emits valid JSON; user still gets (BM25) results.

---

## 8. Configuration additions

Extending the existing `[embeddings]` block from PLAN §8:

```toml
[embeddings]
# Opt-in semantic retrieval layer. Requires the [embeddings] pip extra
# (numpy, httpx) and a running Ollama with nomic-embed-text pulled.
enabled = false

# Ollama connection.
ollama_base_url = "http://localhost:11434"
model = "nomic-embed-text"

# How many FTS5 candidates to rerank. Lower = faster, less chance of
# recovering low-BM25-high-semantic matches. Raise cautiously — rerank
# time scales linearly.
rerank_pool_size = 50

# HTTP timeout for a single Ollama call (embed or probe).
request_timeout_seconds = 10

# Batch size when running `claude-recall embed`. 32-64 is a reasonable
# range on localhost; higher may hurt throughput.
batch_size = 32
```

All have defaults. No `enable_semantic_in_hook` flag — the hook passes `--semantic` unconditionally and the CLI soft-ignores when `enabled=false`.

---

## 9. Module layout

```
src/claude_recall/
├── ...existing...
├── embeddings.py         (NEW — single module)
└── search.py             (modified)
```

Single `embeddings.py` module containing:
- `OllamaClient(base_url, model, timeout)` — thin httpx wrapper
- `embed(text) -> np.ndarray`
- `embed_batch(texts) -> np.ndarray[N, D]`
- `probe() -> ProbeResult` (version, available models, test embed)
- `pack_vector(v) -> bytes`, `unpack_vector(blob, dim) -> np.ndarray`
- `cosine_matrix(query_vec, candidate_matrix) -> np.ndarray[N]`

No separate `vectors.py`. The packing/cosine helpers are < 30 lines each and live alongside the client for locality.

---

## 10. Implementation order

Tests must pass per step before moving to the next, same discipline as the MVP's PLAN §10.

1. **`embeddings.py` — Ollama client + vector ops.**
   - `OllamaClient.embed`, `.embed_batch`, `.probe`
   - `pack_vector`, `unpack_vector`, `cosine_matrix`
   - Tests: httpx mocked for unit tests; optional live integration tests skipped when Ollama is unreachable (detected via `probe()`).

2. **Schema: add `message_vectors` to `SCHEMA_DDL`.**
   - Tests: table exists after `open_db`, cascade delete removes vectors when session deleted.

3. **`claude-recall embed` CLI command.**
   - Handler in `cli.py`. Query for unembedded messages, batch-embed, insert.
   - `--probe` subpath.
   - Tests: end-to-end against a mock OllamaClient injected via a test seam.

4. **`search.py`: `run_search(semantic=False)` param.**
   - Hybrid path: fetch pool (limit=`rerank_pool_size`), embed query, cosine-rank, return top `--limit`.
   - Graceful fallback on Ollama error.
   - Tests: known-corpus case where FTS5 ranks A > B, embeddings flip it because B is semantically closer to the query.

5. **CLI `--semantic` flag on search.**
   - Threads through to `run_search`.
   - Soft-ignore when disabled.
   - Tests: flag presence passes through; flag while disabled warns + runs FTS5-only.

6. **`status` additions.**
   - New JSON fields. Probe Ollama. Count vectors.
   - Tests: JSON shape; agent-context one-liner variations.

7. **Hook scripts add `--semantic`.**
   - Tests: existing budget test still green; emits valid JSON when Ollama unreachable.

8. **Docs update.** CHANGELOG entry, INTEGRATION-GUIDE §9 new rows, ARCHITECTURE §6 revised (mention v0.3 shipped).

Release as `v0.3.0` once §12 passes.

---

## 11. Test plan

New test modules:
- `tests/test_embeddings.py` — unit tests with mocked httpx; optional live tests gated on Ollama availability.
- `tests/test_semantic_search.py` — hybrid retrieval correctness.

Existing test modules gain targeted additions:
- `tests/test_cli.py` — `embed`, `embed --probe`, `search --semantic`, status additions, hook passes `--semantic`.
- `tests/test_storage.py` — `message_vectors` created, cascade on session delete.

**Key test cases:**

- `test_embeddings::test_embed_returns_correct_dimension` — mocked Ollama response; output is `np.float32[768]`.
- `test_embeddings::test_embed_batch_preserves_order` — batch of N texts returns N rows in the same order.
- `test_embeddings::test_probe_detects_missing_model` — Ollama reachable but model not in `/api/tags` → probe reports which step failed.
- `test_embeddings::test_cosine_matrix_matches_manual` — numerical check against hand-computed cosine.
- `test_embeddings::test_pack_unpack_roundtrip` — random vector → bytes → vector, exact equality.
- `test_semantic_search::test_rerank_flips_poor_bm25_winner` — curated corpus where FTS5 picks the wrong result on a semantic-phrasing mismatch; embeddings rerank picks the right one.
- `test_semantic_search::test_soft_ignore_when_disabled` — `--semantic` passed, config disabled, runs FTS5-only + warning on stderr, exit 0.
- `test_semantic_search::test_ollama_down_falls_back_to_fts5` — mocked httpx connection error, CLI exits 0 with FTS5 results and a stderr warning.
- `test_cli::test_embed_respects_project_auto` — `embed --project auto` only embeds the cwd project's messages.
- `test_cli::test_embed_probe_exits_0_on_healthy_ollama` — mocked healthy path.
- `test_cli::test_status_reports_embedding_health` — new JSON fields present with correct values.

**Coverage goal:** ≥ 85% on `embeddings.py`. Existing targets on storage/indexer/search/keywords hold.

**Live integration (optional):** A `pytest.mark.live_ollama` marker. Tests skip by default unless `OLLAMA_LIVE=1` env var is set. Runs one real `embed` call, asserts dimension and non-zero norm. Useful for local sanity before release; never required in CI.

---

## 12. Acceptance criteria — v0.3.0 ship readiness

- [ ] All tests from §11 pass on Python 3.11, 3.12, 3.13 (same matrix as MVP; still deferred to future CI per PLAN §16).
- [ ] `pip install -e ".[embeddings]"` succeeds from a fresh venv. `pip install -e .` (core) still works with zero deps.
- [ ] `claude-recall embed --probe` returns exit 0 against a local Ollama with `nomic-embed-text` pulled.
- [ ] `claude-recall embed` completes in < 5 minutes for ANI-scale (25k messages) on consumer hardware.
- [ ] `claude-recall search "the cascade" --semantic` returns the `feedback loop` conversation when FTS5 alone misses it (verified on an ad-hoc constructed corpus in the test suite, not ANI).
- [ ] `UserPromptSubmit` hook completes in < 500ms on warm-Ollama localhost (not enforced in CI; one-shot measurement at release time).
- [ ] `UserPromptSubmit` hook still under budget and still returns valid JSON when Ollama is unreachable.
- [ ] `claude-recall status` reports all four new fields and the new health check.
- [ ] Hook script stamp bumps to 0.3.0 and stale-hook warning fires on pre-0.3.0 installs.
- [ ] Docs: CHANGELOG entry, INTEGRATION-GUIDE §9 rows, ARCHITECTURE §6 updated to past tense.
- [ ] No network calls when `[embeddings].enabled=false` (re-verified by grepping for httpx usage gated behind a config check).

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Ollama cold model load blows the 500ms hook budget on first prompt** | Expected. The `embed --probe` command pre-warms the model. Documented in INTEGRATION-GUIDE. First-prompt quality regression post-Ollama-restart is accepted. |
| **User enables embeddings, forgets to run `embed`, wonders why semantic search returns nothing** | `status` exposes `messages_without_vectors`. Agent-context line calls it out. `search --semantic` with an empty `message_vectors` table falls back to FTS5 and warns. |
| **Model upgrade (nomic-embed-text v1.5 → v2) changes dimension or semantics** | `message_vectors.model` and `.dim` stored on every row. A future migration step can detect drift and require `embed --rebuild`. Out of v0.3 scope — document the constraint. |
| **25k-message embed job interrupted mid-run** | Insertions commit per batch. Resuming `embed` picks up where it left off via the `LEFT JOIN ... WHERE v.msg_id IS NULL` filter. Idempotent by construction. |
| **httpx adds attack surface / supply-chain risk** | Only loaded when `[embeddings]` extra is installed and `enabled=true`. Core install still has zero runtime deps. Pinned major version in `pyproject.toml`. |
| **Cosine is too coarse — rerank sometimes picks semantically-close-but-topically-unrelated messages** | Pure rerank over a BM25-filtered pool already constrains to topical candidates. If v0.3 real-world use surfaces false positives, v0.3.1 can add RRF (PLAN §2 in this doc, Fork 4 alternative) without data-model changes. |
| **Users on remote Ollama (network-attached model host) blow budget** | Documented. Config flag `request_timeout_seconds` lets them fail fast. The hook always degrades gracefully. |
| **Embed call returns an NaN or zero vector** | `embed_batch` asserts finite + non-zero norm per vector; failures log and skip that message (no vector row inserted; FTS5 path still works for it). |

---

## 14. Security considerations

- **No new network surface beyond `http://localhost:11434`** by default. Users who point `ollama_base_url` elsewhere should understand the implication; documented.
- **Message content is sent to Ollama.** Local-only by default means this is a loopback call; the content never leaves the machine. If the user overrides `ollama_base_url` to a remote host, they are opting into a network send of their session content. Warn prominently in the config comment and INTEGRATION-GUIDE.
- **No secret detection on embed.** Same stance as MVP (PLAN §15). Out of v0.3 scope.
- **httpx TLS verification** left at library defaults for any non-localhost override.

---

## 15. Design decisions resolved (approved 2026-04-23)

Six forks presented during scoping, owner's calls recorded:

1. **Storage = SQLite BLOB (dedicated `message_vectors` table).** Preserves "one file is the whole index" story from PLAN §2. Vector-DB benefits don't materialize at rerank-pool scale.
2. **Embed = hybrid (separate `embed` command, auto-incremental when enabled).** Makes one-time cost visible; non-embedding users pay nothing.
3. **Hook = transparent semantic when enabled.** Ollama accepted as a hard requirement for `[embeddings].enabled=true` installs. `--semantic` unconditional in the shipped hook; CLI soft-ignores when disabled.
4. **Rerank = pure rerank (top-N BM25 → cosine → top-K).** RRF kept in reserve for v0.3.1 if false-positive rate is visible in real use.
5. **Granularity = full-message embeddings.** No chunking. Revisit post-release if long-assistant-turn recall is poor.
6. **Ollama failure modes = graceful.** Ollama required when enabled, but transient failures (down, cold-loading, timeout) never crash — CLI warns + falls back to FTS5; hook emits `{}`. `status` exposes reachability.

---

## 16. Open questions for v0.3.1+

Parking these rather than scope-creeping v0.3:

1. **Chunking for long messages.** Watch for recall failures on 5k+ token assistant turns. If real, chunk on paragraph boundaries with `(msg_id, chunk_index)` keys.
2. **RRF over pure rerank.** If false-positive rate is perceptible in real use, swap the rerank logic (§11 `test_rerank_flips_poor_bm25_winner` stays a regression test).
3. **Model pluggability.** If `nomic-embed-text` is updated (new dimension) or a user wants `all-MiniLM-L6` / etc., add model registry to `embeddings.py`.
4. **Persistent Ollama warming.** A `claude-recall daemon` that holds Ollama in keep_alive=-1 for hook-latency stability. Likely won't be needed; `embed --probe` at SessionStart is cheaper.
5. **Cross-project semantic search.** Orthogonal to v0.3; belongs with v0.4's `--global` flag.
6. **Embedding cache for identical messages.** Most messages are unique but occasional boilerplate may repeat; content-hash → vector reuse. Low-value unless archives grow 10×.

---

## 17. Milestones

- **v0.3.0 — this scope.** Embeddings layer, hybrid retrieval, CLI + hook integration, tests, docs. Release as GitHub tag.
- **v0.3.1 — iterate on real-world use.** RRF if needed, chunking if needed, model pluggability if needed.
- **v0.4.0 — cross-project search** (per PLAN §13). `--all-projects` flag; semantic layer works across scopes for free since it's a rerank over the FTS5 candidate pool.

---

## 18. Starter commands for the implementing agent

```bash
# 1. Branch
git checkout -b v0.3-embeddings

# 2. Install dev + embeddings extras
pip install -e ".[embeddings,dev]"

# 3. Verify Ollama is reachable and model is pulled (manual, once)
curl http://localhost:11434/api/version
ollama pull nomic-embed-text

# 4. Implement in §10 order, running pytest after each module
pytest tests/test_embeddings.py -v
# ... etc

# 5. Pre-release sanity check
claude-recall embed --probe
claude-recall embed --verbose              # on the real archive
claude-recall search "some semantic query" --semantic --format text

# 6. Tag after acceptance
git tag v0.3.0 && git push --tags
gh release create v0.3.0 --notes-file CHANGELOG.md
```

---

**End of v0.3 embeddings plan.** Companion reading in [PLAN.md](PLAN.md) (overall spec) and [ARCHITECTURE.md](ARCHITECTURE.md) §6 (prior rationale for deferring embeddings to v0.3). CHANGELOG.md is updated at release, not in advance.
