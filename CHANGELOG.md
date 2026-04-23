# Changelog

All notable changes to `claude-recall`. Format: one section per tag.

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
