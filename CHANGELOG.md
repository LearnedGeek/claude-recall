# Changelog

All notable changes to `claude-recall`. Format: one section per tag.

## v0.3.0 — 2026-04-23

Optional semantic retrieval layer per [docs/EMBEDDINGS-PLAN.md](docs/EMBEDDINGS-PLAN.md).
Hybrid retrieval (FTS5 candidate pool → embedding rerank). Opt-in via the
`[embeddings]` pip extra and `[embeddings].enabled = true` in config.

### Added

- `claude_recall.embeddings` module (opt-in; requires the `[embeddings]`
  extra). `OllamaClient` for `/api/embed` + `/api/version` + `/api/tags`,
  with finite/non-zero/shape validation. `pack_vector` / `unpack_vector`
  deterministic BLOB codec. `cosine_matrix` vectorized over a candidate pool.
- `message_vectors` SQLite table alongside `messages` (cascade-deleted on
  session re-index). Added unconditionally; unused when embeddings are off.
- `claude-recall embed [--rebuild] [--probe] [--project auto]` command.
  Incremental by default (embeds rows without an existing vector). `--probe`
  exits 0 only on a full Ollama path check (reachable + model pulled + test
  embed succeeds).
- `claude-recall search --semantic` flag. Reranks top-`rerank_pool_size`
  BM25 candidates by cosine against the query embedding. Pure rerank — BM25
  determines the candidate pool, cosine re-orders it, BM25 rank is preserved
  on the result for visibility.
- `status` new fields: `embeddings_enabled`, `ollama_reachable`,
  `vectors_indexed`, `messages_without_vectors`, `checks.embeddings_ready`.
  agent-context output appends `Embeddings: <count> vectors, Ollama reachable`
  when healthy, or an actionable hint when not (`run claude-recall embed`).
- Graceful degradation: Ollama down / model missing / zero vectors all fall
  back to FTS5-only results with `semantic_fallback_reason` set for CLI
  visibility. The hook still emits valid JSON under every failure mode.
- `SearchResult` gains `bm25_rank` and `semantic_rank` (populated only when
  semantic rerank ran).

### Changed

- `--semantic-from-config` flag on `search` respects
  `[embeddings].use_in_hook`. Shipped hook scripts pass this flag so hook
  latency stays on the v0.2.1 fast path by default.
- Hook-stamp version bumped; `status` will flag v0.2.1 hooks as stale on upgrade.

### Known limitation — hook latency

The `UserPromptSubmit` hook was originally designed to run `--semantic`
unconditionally (PLAN §7 Fork 3 Option A). Real measurement showed
semantic-enabled hook path runs ~685ms on warm-Ollama localhost, over the
500ms budget from PLAN §7.3. Dominant cost is numpy + httpx cold-import
(~240ms) plus Python subprocess overhead. v0.3.0 ships with
`[embeddings].use_in_hook = false` default to preserve the 500ms budget
as a hard guarantee. Users who have verified their setup can flip
`use_in_hook = true` in `config.toml` to opt into semantic-in-hook.

The proper fix — a compiled hook binary that eliminates the Python
startup + import cost — lands in v0.4.0. See
`docs/HOOK-BINARY-PLAN.md` (coming with v0.4 work) for the C# NativeAOT
architecture.

### Tests

138 passing. `tests/test_embeddings.py` (21 unit + 1 live-ollama gated on
`OLLAMA_LIVE=1`), `tests/test_semantic_search.py` (6 hybrid retrieval
cases), expanded `tests/test_cli.py` and `tests/test_storage.py`. Coverage:
embeddings 93%, search 97%, projects 94%, keywords 97%, storage 89%,
indexer 85%, config 92%, cli 81%.

### Deviations from PLAN

- PLAN §17 Decision 1 (spacy for keyword extraction) — already deviated
  in v0.2 per the concrete 500ms hook budget.
- EMBEDDINGS-PLAN Fork 3 (hook uses semantic transparently) — partly
  deferred via `use_in_hook=false` default, re-activated in v0.4.0 after
  the C# hook binary ships.

## v0.2.1 — 2026-04-23

Hook-delivery-layer fixes from [issue #2](https://github.com/LearnedGeek/claude-recall/issues/2).
On a multi-project install the v0.2.0 hook surfaced cross-project matches
because it passed no `--project` filter and hardcoded `--days 30` regardless
of `config.toml`. Both fixed here.

### Added

- `claude_recall.projects` module. `slug_from_path(path)` computes the
  Claude Code slug convention (lowercase drive letter, separators → `-`).
  `resolve_project_slug(conn, cwd)` resolves against the indexed sessions
  case-insensitively, returning the actual stored slug when the archive
  uses the older `E--` form.
- `--project auto` on `claude-recall search`. Resolves to the current
  working directory's slug. Shipped hook scripts now pass it.
- `--from-config` flag on `claude-recall search`. When set, unspecified
  flags (`--days`, `--limit`, `--threshold`) default to `[search] hook_*`
  values from `config.toml`. Explicit CLI flags still win. Shipped hook
  scripts now pass it instead of hardcoding values.
- Hook version stamp: `init-hooks` writes `.claude/hooks/.claude-recall-version`.
  `status --format agent-context` surfaces a stale-hook warning when the
  stamp disagrees with the installed package, pointing at `init-hooks --force`.
  `status` JSON exposes `package_version`, `installed_hook_version`, and
  the new `hooks_current` check.

### Changed

- `on_prompt.sh` / `on_prompt.ps1` invocation went from
  `--days 30 --limit 3 --threshold 0.3 --extract-keywords --agent-context`
  to `--project auto --from-config --extract-keywords --agent-context`.
  Tunings in `config.toml` now actually take effect.
- `init-hooks` output now prints the version it wrote.

### Tests

96 passing. `tests/test_projects.py` (7), new CLI cases for `--project auto`,
`--from-config` precedence, and the hook version stamp.

## v0.2.0 — 2026-04-23

Keyword extraction in the `UserPromptSubmit` hook path (PLAN §13 v0.2 item, §17
Decision 1). Natural-language prompts like *"remind me what we decided about
regex patterns"* now strip stopwords/pronouns/fillers before FTS5 sees them,
producing ranked topical hits instead of a noisy OR-join over every token.

### Added

- `claude_recall.keywords` module with `extract_keywords()` and
  `build_fts_query()`. Stopword list tuned for English natural-language
  prompt shapes. Preserves quoted phrases as single keywords.
- `--extract-keywords` flag on `claude-recall search`. Off by default so
  direct CLI users get exact FTS5 semantics; the shipped hook scripts pass
  it automatically.
- 13 new tests in `tests/test_keywords.py`.

### Changed

- Hook scripts (`on_prompt.sh`, `on_prompt.ps1`) now pass
  `--extract-keywords` to `claude-recall search`.
- `UserPromptSubmit` hook still completes within the 500ms budget (§7.3).

### Deferred from PLAN §17 Decision 1

PLAN named spacy as the preferred extraction backend. During v0.2 scoping,
spacy's 1–2s cold-import per fresh-process hook invocation conflicted with
the 500ms budget (§7.3). The PLAN's own exception clause (*"unless a
concrete reason emerges during v0.2 scoping"*) authorized an alternative.
v0.2 ships a stdlib-only extractor that stays in budget and delivers the
primary natural-language improvement the dogfooding report called for. A
warm-daemon spacy path is open for v0.3 as an opt-in `[nlp]` extra.

## v0.1.1 — 2026-04-23

### Fixed

- `UserPromptSubmit` hook returning `{}` on most natural-language prompts
  ([issue #1](https://github.com/LearnedGeek/claude-recall/issues/1)).
  `_execute_with_fallback` now retries with the OR-joined sanitized form
  on a zero-row raw result, not only on parse failure. BM25 ranking
  preserved.

## v0.1.0 — 2026-04-23

Initial MVP release. Indexer, CLI, hooks, tests all green. See
[docs/PLAN.md §12](docs/PLAN.md) for the acceptance-criteria report.
