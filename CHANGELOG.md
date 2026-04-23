# Changelog

All notable changes to `claude-recall`. Format: one section per tag.

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
