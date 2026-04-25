# Changelog

All notable changes to `claude-recall`. Format: one section per tag.

## v0.5.4 — 2026-04-25

Critical hook-shape fix for [issue #15](https://github.com/LearnedGeek/claude-recall/issues/15).
Through v0.5.3, `init-hooks` generated the deprecated flat hook shape
`{command, matcher}` instead of the schema-required nested shape
`{matcher?, hooks: [{type, command, ...}]}`. Claude Code's parser used
to accept both leniently; the strict-validation pass introduced in
[Claude Code v2.1.118](https://github.com/anthropics/claude-code/releases/tag/v2.1.118)
(April 23, 2026, same release that added `type: "mcp_tool"` hooks)
rejects the flat shape outright with the message:

> Settings file failed to parse `<repo>/.claude/settings.json` —
> Expected array, but received undefined. Permission rules and other
> settings from this file are not in effect.

This **silently disabled the host project's settings as a side effect**
— not just claude-recall's hooks, but every permission rule, env var,
and unrelated hook in the same file. Anyone who ran `init-hooks` from
v0.4 onward on Windows is affected.

### Migration

```
pip install --upgrade claude-recall
claude-recall init-hooks --force
```

`--force` rewrites the two events claude-recall manages
(`SessionStart`, `UserPromptSubmit`); other hook events are preserved.

### Fixed

- **`init-hooks` now emits the schema-correct nested shape.** Each
  matcher entry contains a `hooks: [{type: "command", command: "..."}]`
  array, matching the canonical Claude Code settings schema.
- **`shell: "powershell"` is set on `.ps1` hooks.** Without this, bash
  (the default hook shell) tries to execute the raw `.ps1` path as a
  binary and fails silently — even with the shape fix in place. Other
  shells fall through to the default.
- **De-dup logic walks the nested `hooks` array** when checking
  whether a command is already registered, so re-running `init-hooks`
  without `--force` doesn't double-add entries.

### Tests

- New regression test
  (`test_init_hooks_emits_schema_correct_nested_shape`) asserts that
  `command` never appears at the top level of a matcher entry, that
  `hooks` is always a list with `type: "command"` inner entries, and
  that `.ps1` commands carry `shell: "powershell"`.
- Existing init-hooks tests updated to assert against the new shape
  via a shared `_hook_commands()` helper.

## v0.5.3 — 2026-04-24

Dogfooding dispatch for [issue #13](https://github.com/LearnedGeek/claude-recall/issues/13) —
`--project` filter returning zero matches for an indexed CrewTrack
session despite `list` showing it with 5622 turns. This release ships
one confirmed fix (case-insensitive project filter) plus the
diagnostic tooling needed to pinpoint the actual root cause of OC's
specific symptom (which was not casing).

### Fixed

- **`--project <slug>` filter is now case-insensitive** across
  `search`, `list`, and `embed`. Both `E--Foo` and `e--foo` forms now
  match whatever case is stored. The `--project auto` path already
  normalized via `projects.resolve_project_slug`; the explicit form
  did not. (Not the root cause of #13 — OC ruled out casing — but the
  right behavior regardless, and cheap to fix at the same time.)

### Added

- **`claude-recall status --integrity-check`** — runs consistency
  queries against the index and prints:
  - Global row counts: sessions / messages / messages_fts
  - Flags mismatches between messages and messages_fts (the
    trigger-didn't-fire case)
  - Per-session: stored `turn_count` vs actual messages-row count
    (catches "session exists but has no joinable messages" — the
    leading hypothesis for #13)
  - Per-project breakdown: sessions, stored turns, actual messages,
    vectors
  - Orphan message detection (session_id pointing at missing session)

  Use this when `--project` returns zero and you can't figure out why.

### Tests

4 new regression cases:
- `search --project` mixed-case matches correctly
- `list --project` mixed-case matches correctly
- `status --integrity-check` reports global + per-project counts
- `status --integrity-check` flags FTS row-count mismatch (simulates
  the trigger-missed-inserts hypothesis)

Full suite: 156 Python + 62 C# = 218 passing.

### Not yet fixed

The root cause of [#13](https://github.com/LearnedGeek/claude-recall/issues/13)
on OC's specific archive. Casing was ruled out; other hypotheses
(session row without matching messages, FTS row mismatch, orphan state)
can now be confirmed or disproven with one `claude-recall status
--integrity-check` against OC's DB. Fix will land in v0.5.4 once the
actual failure mode is identified.

### Upgrading

```bash
pip install --upgrade 'claude-recall[embeddings]'
```

No `init-hooks --force` needed — no hook-binary changes in this release.

## v0.5.2 — 2026-04-24

Fixes the four issues filed against v0.5.1's PyPI landing-page and hook
behavior ([#9](https://github.com/LearnedGeek/claude-recall/issues/9), [#10](https://github.com/LearnedGeek/claude-recall/issues/10), [#11](https://github.com/LearnedGeek/claude-recall/issues/11), [#12](https://github.com/LearnedGeek/claude-recall/issues/12)) plus one UX paper-cut (init-hooks
nudges on upgrade).

### Fixed

- **#9 — PyPI README was telling users to go hunt wheel URLs.** The `README.md`
  still carried v0.4.2 pre-PyPI framing (*"v0.5 will publish to PyPI ...
  Until then, install from a tagged GitHub Release wheel"*). Rewritten
  for v0.5.x: `pip install 'claude-recall[embeddings]'` is the landing
  install command, status block reflects beta-currently-shipping state,
  "Known Issues" section rolled forward through #8.
- **#10 — relative markdown links 404'd on PyPI.** Every `[label](docs/...)`
  or `[label](CHANGELOG.md)` link in the README now points at absolute
  `https://github.com/LearnedGeek/claude-recall/blob/main/...` URLs.
  Renders correctly on both PyPI and GitHub.
- **#11 — hook latency claim was calibrated for small archives.** Added
  `[embeddings].keep_alive` config (default `"30m"`) — sent on every
  Ollama embed call so the model stays resident across a coding session
  instead of unloading every 5 minutes (Ollama default). Eliminates the
  ~4s cold-load on the first prompt after any idle gap.
  Also added a `--timing` flag to the hook binary for per-stage latency
  self-diagnosis (stderr-only; never pollutes hook stdout).
  README hook-latency section rewritten with honest scaling numbers
  (~80ms on small archives, ~2s on 25k-message archives with semantic
  on). The docs now match dogfooding observation.
- **#12 — C# hook binary `--version` reported stale `0.4.0`.** Version
  now flows from `pyproject.toml` into the `dotnet publish /p:Version=X.Y.Z`
  property at build time, and `Program.Version` reads it from
  `AssemblyInformationalVersionAttribute` instead of a hardcoded
  constant. No more manual bumping of two files; no more drift.
  Build verified: `claude-recall-hook.exe --version` now reports `0.5.2`.
- **Bonus — `init-hooks` stopped nagging upgraders with first-install
  instructions.** The "run `claude-recall index`" and "to enable
  semantic rerank, edit config.toml" nudges now print conditionally:
  only when the index is empty / embeddings are off. Upgrade runs of
  `init-hooks --force` on a working install stay quiet.

### Added

- `[embeddings].keep_alive` config key (default `"30m"`). Set to `"0"`
  to unload after every call, `"-1"` to keep loaded indefinitely, or
  any Ollama-accepted duration string.
- `claude-recall-hook.exe --timing`: prints per-stage latency breakdown
  to stderr (`startup+config`, `stdin`, `keywords`, `db-open`,
  `slug-resolve`, `fts5`, `rerank`, `json-out`). Use for self-diagnosis
  when hook latency feels off.
- `build-hook.ps1` and `.github/workflows/build-wheels.yml` both now
  read the package version from `pyproject.toml` and pass it as the
  `dotnet publish /p:Version=...` property.

### Changed

- README completely reframed for the PyPI-shipping state. Landing page
  now leads with `pip install 'claude-recall[embeddings]'` and absolute
  links to docs.
- Default `[embeddings].keep_alive = "30m"` — was previously omitted
  (relying on Ollama's 5m default), which caused measurable cold-model
  re-loads for users with more than a few minutes between prompts.

### Tests

152 Python + 62 C# = 214 passing, unchanged. `Version_IsSemVer`
loosened to `Version_IsNotEmpty` so the local `dotnet test` path
(which hits the `0.0.0-dev` fallback when `/p:Version=` isn't passed)
still exercises the new reflection-based version lookup.

### Upgrading

```bash
pip install --upgrade 'claude-recall[embeddings]'
claude-recall init-hooks --force
```

The `--force` refreshes the hook binary in your project's
`.claude/hooks/` so `claude-recall-hook.exe --version` now correctly
reports 0.5.2 instead of the stale 0.4.0. No data changes; no re-embed
needed.

## v0.5.1 — 2026-04-24

CI-only patch. v0.5.0's tag push failed workflow parsing because GitHub
Actions disallows `secrets.*` references in step-level `if:` conditions
(I had guarded the new `publish-pypi` step with
`if: ${{ secrets.PYPI_API_TOKEN != '' }}` for fork-safety, which is an
invalid expression that breaks the whole workflow). The `build-wheel-*`
jobs never ran, and nothing was uploaded to PyPI or attached to the
GitHub Release.

v0.5.1 is the intended v0.5.0 content with the workflow guard removed.
The underlying fork-safety concern is instead handled by letting
`pypa/gh-action-pypi-publish` fail with a clear error message when no
token is set — which is the right behavior for a release operation
anyway.

No code changes vs v0.5.0 tag. Same pyproject polish, same CHANGELOG
narrative from v0.5.0 below, same "first PyPI publish" milestone —
just an extra patch-version tick for the CI fix.

## v0.5.0 — 2026-04-24 (CI parse failure, no artifacts published)

**Distribution milestone: first PyPI publish.** `pip install claude-recall` now
works globally. No user-visible behavior changes vs v0.4.3; this release
exists to shift distribution off `git+https://...` wheel URLs and onto the
standard Python package index.

### Added

- **PyPI publish via CI.** `.github/workflows/build-wheels.yml` gains a
  `publish-pypi` job that uploads both the `win_amd64` (binary-bundled)
  and `py3-none-any` (pure-Python fallback) wheels to PyPI on tag push,
  using the official `pypa/gh-action-pypi-publish` action. Gated on the
  `PYPI_API_TOKEN` secret so forks / pre-setup branches don't turn CI red.
- `pyproject.toml` polish: Development Status bumped `Alpha → Beta`,
  classifier list expanded (OS coverage, topic tags, Typed trailer),
  keyword list extended for PyPI search discoverability, `Project-URL`
  entries for Repository / Documentation / Changelog / Releases.
- `docs/POST-STRATEGY.md` — distribution plan matched to the owner's
  explicit constraints (no HN, no video, minimal social). Ordered channel
  list with leverage rationale.
- `docs/v0.5-PYPI-SETUP.md` — one-time PyPI account setup checklist for
  the project owner.

### Install

```bash
pip install 'claude-recall[embeddings]'
claude-recall init-hooks
claude-recall index
```

Windows x64 automatically gets the NativeAOT hook binary via the platform
wheel; other platforms get the pure-Python shell-hook fallback per v0.4's
distribution design. Both wheels ship with every tag-triggered CI run.

### Not changed

- No C# hook binary changes. 62 xUnit tests green unchanged.
- No semantic-retrieval or hook-correctness logic changes. All 8
  dogfooding issues remain closed.
- Tests: 152 Python + 62 C# = 214 passing. Same surface as v0.4.3.

### Upgrading from any v0.4.x

```bash
pip install --upgrade 'claude-recall[embeddings]'
claude-recall init-hooks --force
```

That's it. No re-embed needed; the vector store and index file are
untouched. The `--force` is only necessary if you want `init-hooks` to
clean up any duplicate `UserPromptSubmit` entries left over from the
v0.4.0 → v0.4.1 upgrade path (fixed in v0.4.2); otherwise you can omit it.

### What's next

Per [docs/POST-STRATEGY.md](docs/POST-STRATEGY.md):
- Blog post on learnedgeek.com + dev.to cross-post
- GitHub Discussion in `anthropics/claude-code`
- Awesome-list PRs (awesome-claude-code, awesome-rag, awesome-ollama)
- Newsletter pitches (Python Weekly, Simon Willison)
- LinkedIn post
- r/ClaudeAI post

Pre-1.0 cadence continues — v0.5.x for patches, v0.6.0 for the next
substantial feature (cross-project `--all-projects` search is the
PLAN §13 v0.4 item that's been deferred since the hook-binary work).

## v0.4.3 — 2026-04-24

First-run embeddings-setup UX cluster. Four issues ([#5](https://github.com/LearnedGeek/claude-recall/issues/5), [#6](https://github.com/LearnedGeek/claude-recall/issues/6), [#7](https://github.com/LearnedGeek/claude-recall/issues/7), [#8](https://github.com/LearnedGeek/claude-recall/issues/8))
from the ANI dogfooder, filed together because individually they were
low-medium severity but cumulatively killed the turn-on-semantic-rerank
experience. #8 was silent data loss (27% of messages) so treated as the
real headliner even though severity ratings were uneven.

### Fixed

- **#5 — `status`: `ollama_reachable` is now honest.** The json/text
  status probes Ollama unconditionally so users troubleshooting an
  embeddings-disabled setup can see reachability as standalone diagnostic
  information. agent-context output (the SessionStart hook path) still
  skips the probe when embeddings are off to preserve the 2s budget.
  `embeddings_ready` remains the end-to-end conjunction.
- **#6 — `init-hooks` scaffolds a `config.toml` template** with every
  section commented-out and a detailed `[embeddings]` block including
  the four-step turn-on procedure. Never touches an existing config.
  First-run output now ends with a one-liner pointing at the
  INTEGRATION-GUIDE §9 "How do I turn on embeddings?" row.
- **#7 — `embed --probe` handles cold-start.** Probe timeout is now
  `max(config_timeout, 30s)` so a 4s model cold-load doesn't register as
  a network error. When the probe fails with `reachable=true` +
  `model_present=true` + `"time"` in the error string, the error is
  rewritten as *"First-call model load can take several seconds; retry
  once the model is warm"* — actionable instead of misdirecting users
  into debugging firewalls.
- **#8 — `embed` no longer loses 27% of the corpus when one message is
  oversized.** Three-layer fix:
  - New `[embeddings].max_input_chars` config (default 6000,
    ~1500 tokens for English) — per-message truncation before Ollama
    sees the batch. Eliminates the 400 Bad Request from context-length
    overflow for essentially all typical inputs.
  - `embed_batch` surfaces Ollama's response body in the
    `EmbeddingError` message so the cause (*"input length exceeds the
    context length"*) is visible without reading Ollama's server log.
  - Per-batch failure fallback: when a batch still fails after
    truncation (e.g., a weird encoding issue), `embed` retries each
    message individually instead of dropping all 32. New `dropped`
    count in the summary line; exit 2 only when drops are non-zero.

### Added

- Per-input character limit configurable via
  `[embeddings].max_input_chars = 6000`.
- `embed` summary line reports `N message(s) dropped` when the
  per-message fallback couldn't recover some inputs.

### Tests

8 new regression tests across the four issues: probe-when-disabled,
agent-context-skips-probe, probe-cold-start-timeout-and-message,
config-scaffold-on-first-run, config-preserves-existing, truncation,
error-body-surfacing, singleton-fallback. Full suite: 152 Python + 62
C# = 214 passing.

### Upgrading

```bash
pip install --upgrade "claude_recall[embeddings] @ https://github.com/LearnedGeek/claude-recall/releases/download/v0.4.3/claude_recall-0.4.3-py3-none-win_amd64.whl"
claude-recall init-hooks --force
# If embeddings were enabled on v0.4.2 and had drops, re-embed:
claude-recall embed --rebuild --verbose
```

The `--rebuild` is worth running if you hit the v0.4.2 silent-drop
issue (confirm via `claude-recall status | grep messages_without_vectors`
— if it's non-zero when `vectors_indexed > 0`, you're missing vectors).
v0.4.3 will successfully embed everything that previously failed.

## v0.4.2 — 2026-04-23

Bugfix for [issue #4](https://github.com/LearnedGeek/claude-recall/issues/4):
`init-hooks --force` used to merge into existing `settings.json` instead of
overwriting the managed hook events. Users who manually wired a stale path
around v0.4.0's crash (issue #3), then ran v0.4.1's documented upgrade
command, ended up with two `UserPromptSubmit` entries — both pointing at
the same binary via different filesystem paths — and the hook firing twice
per prompt.

### Fixed

- `--force` now wipes `hooks.SessionStart` and `hooks.UserPromptSubmit`
  before re-merging, matching the release-note contract
  ("overwrite ... and save the hand-edit to `settings.json.bak`"). Hook
  events the tool doesn't manage (`PreToolUse`, `PostToolUse`, etc.) are
  always preserved, regardless of `--force`.
- Without `--force` the merge behavior is unchanged — v0.3.x-era users
  layering their own hook entries under our managed events keep working.
- Summary line adjusted: `--force` says "rewritten into settings.json",
  merge path says "merged into settings.json".

### Tests

- 2 new CLI regression tests cover: (a) `--force` with stale entries
  under managed events + user entries under non-managed events produces
  exactly one entry per managed event and preserves the non-managed ones,
  (b) non-force preserves pre-existing user entries under managed events.
  Full suite: 144 Python + 62 C# = 206 passing.

### Upgrading from v0.4.0 or v0.4.1

```bash
pip install --upgrade "claude_recall[embeddings] @ https://github.com/LearnedGeek/claude-recall/releases/download/v0.4.2/claude_recall-0.4.2-py3-none-win_amd64.whl"
claude-recall init-hooks --force
```

If you already have a duplicate-entry `settings.json` from the v0.4.1
upgrade, `init-hooks --force` on v0.4.2 cleans it up: the pre-existing
file is backed up to `settings.json.bak`, and the new `settings.json`
contains exactly one entry per managed hook event.

## v0.4.1 — 2026-04-23

Bugfix for [issue #3](https://github.com/LearnedGeek/claude-recall/issues/3):
v0.4.0 wheels shipped without any `hooks/*.ps1` or `hooks/*.sh` files
because the `package-data` glob in `pyproject.toml` only listed `native/*`.
`claude-recall init-hooks --force` crashed with an unhelpful
`FileNotFoundError` stack trace on every v0.4.0 wheel install.

### Fixed

- `pyproject.toml`: `claude_recall` package-data now includes
  `hooks/*.ps1` and `hooks/*.sh`, so wheels actually contain the shell
  scripts that existed in the repo all along. v0.4.0 source installs
  (`pip install -e .`) worked by accident; wheel installs did not.
- `claude-recall init-hooks` checks each expected source file before
  copying instead of assuming it exists. Missing `on_prompt.{ps1,sh}` on
  a binary-present wheel is OK (binary handles UserPromptSubmit); missing
  `session_start.{ps1,sh}` emits a stderr warning and skips the hook
  rather than crashing. Missing *both* binary and `on_prompt` exits 1
  with an actionable message naming both expected paths.

### Tests

- 3 new CLI tests cover: (1) wheel missing everything → clean exit 1,
  (2) binary-only wheel → UserPromptSubmit wired, SessionStart warned,
  (3) pure-Python wheel → shell hook wired. 141 Python tests passing
  (+ C# 62).

### Upgrading from v0.4.0

```bash
# Windows x64
pip install --upgrade "claude_recall[embeddings] @ https://github.com/LearnedGeek/claude-recall/releases/download/v0.4.1/claude_recall-0.4.1-py3-none-win_amd64.whl"
claude-recall init-hooks --force

# macOS / Linux
pip install --upgrade "claude_recall[embeddings] @ https://github.com/LearnedGeek/claude-recall/releases/download/v0.4.1/claude_recall-0.4.1-py3-none-any.whl"
claude-recall init-hooks --force
```

If you hit the `FileNotFoundError` on v0.4.0 and manually wired
`settings.json` to bypass it, `init-hooks --force` on v0.4.1 will
overwrite your hand-edit with the correct generated content and write a
`settings.json.bak` of the manual version.

## v0.4.0 — 2026-04-23

Compiled C# hook binary replaces the Python-CLI-based UserPromptSubmit path
per [docs/HOOK-BINARY-PLAN.md](docs/HOOK-BINARY-PLAN.md). Semantic-in-hook
now ships as the default because the 500ms budget constraint is gone.

**Measured hook latency on warm-Ollama localhost:**

| Hook | v0.3 (Python) | v0.4 (C# NativeAOT) | Speedup |
|---|---|---|---|
| FTS5 only | ~125ms | ~17ms | 7× |
| Semantic-on | ~685ms | **~80ms** | **8×** |

### Added

- `src/ClaudeRecall.Hook/` — C# NativeAOT project (~1,000 LOC) with
  `Projects`, `Keywords`, `Config`, `Storage`, `Embeddings`, `Vectors`,
  `Rerank`, and `Program` modules. 62 xUnit tests with parallel structure
  to the Python tests they mirror.
- `src/ClaudeRecall.Hook.Tests/` — xUnit test project.
- `src/ClaudeRecall.sln` — solution file covering both C# projects.
- `src/claude_recall/native/` — package-data slot for the built binary.
  GH Actions win-x64 wheels ship it; pure-Python wheels fall back to the
  v0.3 shell-hook path.
- `build-hook.ps1` — local dev build script. Auto-detects MSVC BuildTools
  and sets PATH/INCLUDE/LIB for the PowerShell session.
- `.github/workflows/build-wheels.yml` — tag-triggered wheel build. Two
  wheels per release: `win_amd64` (binary bundled) + `py3-none-any`
  (fallback).
- `CONTRIBUTING.md` — setup notes, including MSVC requirement for C# work.
- `--semantic-from-config` in `search` (v0.3 had this; formalized and
  used as the hook-side toggle).

### Changed

- `claude-recall init-hooks` detects the bundled binary on Windows wheels
  and registers `claude-recall-hook.exe` directly in `settings.json` as
  the UserPromptSubmit command — no shell wrapper. SessionStart stays
  as a shell script (not latency-critical).
- `[embeddings].use_in_hook` default flipped from `false` → `true`. The
  v0.3 workaround is now the exception; users on non-Windows wheels (pure
  Python, slow hook) should flip it back to `false`.

### Binary details

- **Size**: 6.6MB `claude-recall-hook.exe` + 1.7MB `e_sqlite3.dll`
  (bundled SQLite with FTS5 enabled). Self-contained — no .NET runtime
  install needed on the end user's machine.
- **Trust surface**: Microsoft.Data.Sqlite, SQLitePCLRaw.bundle_e_sqlite3,
  Tomlyn. All pinned.
- **Hook failsafe**: any unhandled exception in the binary is caught and
  `{}` + exit 0 is emitted. Same contract as the Python hook.
- **Byte-identical output**: validated by a cross-runtime test that
  invokes both paths on the same fixture and diffs the `additionalContext`
  field.

### Distribution

Not on PyPI yet — v0.5 target. Install from the v0.4.0 GitHub Release wheels:

- **Windows x64** (binary bundled):
  ```bash
  pip install --upgrade "claude_recall[embeddings] @ https://github.com/LearnedGeek/claude-recall/releases/download/v0.4.0/claude_recall-0.4.0-py3-none-win_amd64.whl"
  claude-recall init-hooks --force   # picks up the new binary + stamps version
  ```
- **macOS / Linux** (pure-Python shell-hook fallback):
  ```bash
  pip install --upgrade "claude_recall[embeddings] @ https://github.com/LearnedGeek/claude-recall/releases/download/v0.4.0/claude_recall-0.4.0-py3-none-any.whl"
  claude-recall init-hooks --force
  ```

> **Don't use `pip install git+https://...`** for v0.4 upgrades — that builds
> from source and the NativeAOT binary is CI-built package data, not part
> of the source tree. A `git+https` install produces a wheel with no binary,
> and `init-hooks` falls back to the v0.3 shell hook path silently.
> Use the release wheel URLs above to get the binary.

### Tests

Python: 139 passing, 1 live-Ollama skipped. C#: 62 passing. Combined 201.

### Deviations from plan

None substantive. Every decision in `docs/HOOK-BINARY-PLAN.md §15` held up
through implementation.

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
