# claude-recall — Architecture

**Companion to [PLAN.md](PLAN.md).** Reads best after skimming the plan. Focuses on the *why* behind choices the plan treats as given.

---

## 1. Design principles

1. **Read-only against the archive.** The `.jsonl` files Claude Code writes are the source of truth. Nothing `claude-recall` does should modify them. This is a one-way indexing relationship.
2. **Zero runtime dependencies for core.** The moment the tool pulls in a heavy dep (spacy, sentence-transformers, a vector DB), install friction kills adoption and the project becomes responsible for dep upgrades. Core stays stdlib-only.
3. **Graceful degradation everywhere.** A malformed JSONL line, a missing config, a crash mid-hook — none of these may block the user's Claude Code session. Every failure mode collapses to "silent no-op" by default.
4. **Obvious observable behavior.** `status`, `list`, `--verbose` — the user should never be left wondering what the tool is doing. Debuggability is a feature.
5. **Composable primitives over packaged workflows.** The CLI does cheap queries; the hooks compose those queries. A user who wants something custom can invoke the CLI themselves without fighting the hook shell.

---

## 2. Why SQLite FTS5

**Alternatives considered:**

| Option | Why rejected |
|---|---|
| Grep over `.jsonl` files | Slow at scale. No ranking. No stemming. User-facing latency > 1s once archive gets non-trivial. |
| Whoosh | Pure Python, no-dep, reasonable FTS. Rejected because it requires a managed index directory and lags SQLite FTS5 on query speed. Also: another thing to learn for maintainers. |
| Lucene / Tantivy via PyO3 binding | Fast but heavy. Install friction. Compiled extension on Windows is fragile. |
| Postgres with `pg_trgm` or `tsvector` | Over-engineered. Requires Postgres install. |
| Weaviate / Qdrant / Chroma | Vector DB — solves a different problem (semantic). MVP doesn't need semantic. Adding a vector DB as primary store would force users to install and manage it. |
| Plain Python dict + pickle | No persistence across runs. No FTS. Not a real option. |

**SQLite FTS5 wins because:**
- Ships in Python stdlib `sqlite3` (3.7+ on most platforms; check at startup).
- Sub-millisecond query over millions of rows.
- Porter stemming + Unicode tokenizer built in.
- BM25 ranking built in.
- Single file on disk; trivial to move, back up, delete.
- Transactions = safe against interrupted index runs.
- Well-understood by any Python dev.

**Caveats acknowledged:**
- No semantic search. Handled by the optional v0.3 embeddings layer.
- FTS5 query syntax differs from natural language; users may trip on it. Documented with examples.
- FTS5 is not standard on all SQLite builds (very old Linux distros). The `status` command checks `fts_available` and reports clearly.

---

## 3. Why two hooks instead of one

**The alternative** would be one SessionStart hook that injects some generic "here's what you were working on recently" context. This is what auto-memory does for Copilot CLI.

**Two hooks work better for Claude Code because:**

- **SessionStart alone is blunt.** Without knowing what the user is about to ask, a SessionStart injection is guessing. It either dumps too much context ("everything from the last week") and wastes tokens, or guesses wrong and wastes the injection.
- **UserPromptSubmit fires per-prompt, with the prompt available.** The prompt itself is the query. Ranked FTS5 retrieval against the prompt text is high-precision by construction — we only inject matches when the prompt semantically aligns with prior content.
- **SessionStart remains useful as a priming signal.** It injects a one-line status ("claude-recall: 87 sessions, most recent Apr 22") that tells the model the archive is searchable and roughly how much is there. This makes the model more likely to reason about prior sessions as a resource. It does not inject content; it injects awareness.

**Why not use the prompt-scoped query exclusively?** Because the SessionStart line is cheap (~30 tokens) and does actual cognitive work — it gives the model a cue that "look at prior sessions" is an available strategy, which it otherwise wouldn't know. Two hooks for ~30 tokens each + conditional content injection is the right shape.

---

## 4. Why incremental indexing

An archive of 100+ sessions with tens of thousands of messages would take multiple seconds to re-index from scratch every SessionStart. That violates the `≤ 2 seconds` hook budget.

**Mechanism:**
- `sessions.file_mtime` stores the mtime at last parse.
- On re-index, compare current `mtime` to stored. Skip if equal.
- If a file has been modified (session still active, messages appended), DELETE that session's existing messages and re-parse the whole file. This is simpler than line-level diffing and correct.

**Performance profile:**
- 100 sessions, 0 changed: ~50ms (just `mtime` checks).
- 100 sessions, 1 changed (active session): ~100ms.
- Full rebuild: ~3 seconds for 10k messages.

---

## 5. Why `additionalContext` injection via stdin JSON

Claude Code's hook system reads stdout JSON from hook scripts and merges recognized fields into the session context. The `additionalContext` field is the standard way to inject text that becomes part of the model's context.

**Other fields the hook protocol supports** (relevant but not used in MVP):
- `systemMessage` — hard inject, shown to the user
- `suppressOutput` — silence hook's stdout for the user
- `decision: "block"` — prevent the prompt from running (not desired here)

We use only `additionalContext` because:
- Silent by default (user sees normal flow, model has extra context).
- Reversible (if output is empty, nothing changes).
- Scoped (only affects the current prompt's context).

---

## 6. Why no embeddings in MVP

**The argument for** embeddings: semantic retrieval finds prior conversations about a concept even when the exact keywords differ. "We talked about the cascade on Tuesday" when the prior session used "feedback loop" — FTS5 misses this; embeddings catch it.

**The argument against** in MVP: it adds dependencies (numpy, httpx, Ollama as a runtime requirement), it's slower per-query, and it's harder to reason about. The FTS5 baseline with Porter stemming handles more overlap than users expect.

**Decision:** Ship FTS5 first. Observe where it falls short in real use. Add embeddings in v0.3 as an optional layer that *augments* FTS5 (hybrid retrieval: FTS5 produces candidates, embeddings rerank), not replaces it.

**Why `nomic-embed-text` specifically** when we do add it:
- Already running on most ANI users' Ollama installs.
- Open weights, runs locally.
- Output dimension (768) is manageable in SQLite as a BLOB or a separate vector file.
- Matches the embedding model ANI itself uses — operational consistency.

---

## 6a. Why stdlib keyword extraction in v0.2 (not spacy)

PLAN §17 Decision 1 named spacy as the preferred keyword-extraction backend,
with an explicit exception: *"unless a concrete reason emerges to prefer an
alternative during v0.2 scoping."*

**The concrete reason:** spacy's cold-import (~1–2 s per fresh Python process,
with the small model loaded) conflicts with the 500 ms `UserPromptSubmit`
budget from §7.3. Each hook invocation is a new process — there is no warm
model cache to amortize against. The hook would either miss budget every time
or be forced to skip extraction on slow-import runs.

**v0.2 decision:** ship a stdlib stopword + token extractor. This is a large
step up from v0.1.1's OR-join-every-token fallback: filler words
(*remind, me, what, we, about*) no longer join the OR expression, so BM25
ranks on topical signal. Still zero runtime dependencies. Still in budget.

**What's preserved for a future opt-in:** a `[nlp]` extra with a warm-daemon
spacy path is the natural v0.3 or v0.4 layer. Running spacy as a long-lived
service alongside the hook, or as a separate indexing-time enrichment, would
re-enable the POS-tagged quality path without paying cold-import cost per
prompt. Out of v0.2 scope.

**Quality-over-efficiency still holds.** The principle from §17 was about
recall quality vs. install/runtime *footprint*. Per-prompt *latency* is a
separate constraint the PLAN also commits to. Stdlib extraction honors both.

---

## 7. Why PowerShell + bash hooks

Claude Code runs on Windows, macOS, and Linux. A bash-only hook breaks Windows users.

**Options considered:**
- Hook in pure Python (single cross-platform script). Rejected because Python hooks add startup cost (~200ms cold) that eats into the UserPromptSubmit budget.
- Node hook. Rejected because Node isn't guaranteed on Claude Code installs.
- Binary hook (Rust, Go). Rejected because it complicates distribution and platform builds.
- Shell hooks per-platform. Chosen because bash and PowerShell are guaranteed available on their respective platforms, and the hook body is tiny — just a CLI invocation and stdin/stdout glue.

`init-hooks` detects platform and installs the appropriate file.

---

## 8. Why TOML config over JSON or YAML

- **TOML** is stdlib as of Python 3.11 (`tomllib` for read). Zero dep.
- **JSON** for config is ugly (no comments, strict syntax).
- **YAML** would require a dep (`pyyaml`), adds install friction, worse error messages on typos.

Config is small enough (< 30 lines) that TOML's lack of programmable config isn't a concern.

---

## 9. Why the database location is `~/.config/claude-recall/`

**Options:**
- `~/.claude/claude-recall/` — colocate with Claude Code data. Rejected because it mixes user data with Claude Code's own state directory; surprising if the user deletes `~/.claude/` to reset.
- `~/.local/share/claude-recall/` — XDG-compliant data dir. Considered. Rejected because on Windows it maps to odd paths.
- `~/.config/claude-recall/` — XDG-compliant config dir, but we're putting data here. Pragmatic choice; `config.toml` lives here too. Documented caveat.

**Windows mapping:** `%APPDATA%/claude-recall/` via `platformdirs`-equivalent pure-stdlib logic.

Override always available via `--db-path` flag or `[database].path` config.

---

## 10. Observability

**What the user sees:**
- `status --verbose` — full index health report.
- `list` — recent sessions with metadata.
- `--verbose` on `index` — per-file progress.
- Logs go to stderr so hook stdout stays clean.

**What the logs contain:**
- `claude-recall index` logs include per-file status (new / updated / skipped / malformed_line_count).
- On fatal error, the traceback goes to stderr; exit code indicates type.
- No telemetry in MVP. No network calls in MVP (the `embeddings` extra is the sole exception and it's opt-in).

---

## 11. Failure mode catalog

| Failure | What happens | User impact |
|---|---|---|
| Archive root doesn't exist | `index` exits 1 with clear message | User must check path or set `--archive-root` |
| Database locked (concurrent use) | SQLite retries with backoff; timeout after 5s → exit 2 | Rare in single-user case; hooks serialize via file lock |
| Malformed JSONL line | Line logged to stderr, skipped | No crash; `--verbose` shows counts |
| Bad JSON output from hook | Claude Code discards hook output | No injection; no visible error |
| Hook takes too long | Claude Code timeout; hook killed | No injection; user sees normal prompt flow |
| FTS5 not available in SQLite build | `status` reports `fts_available: false`; `search` exits 2 with install guidance | Very rare; documented |
| Python < 3.11 at install | `pyproject.toml` `requires-python` rejects install | Early failure with clear reason |
| Session being actively written | File mtime bumps each turn; next SessionStart re-indexes. No race — we use atomic transactions per file. | Occasional brief inconsistency between hook run and active session state; acceptable. |

---

## 12. Open architectural questions for v0.2+

1. **Cross-project search.** Archive has multiple project dirs. Is there value in a `--global` query that spans all? Probably yes (insight from Project A informs Project B), but it needs a UX for disambiguation. Deferred to v0.4.
2. **Checkpoint-style summaries.** auto-memory mentions that Copilot writes periodic summaries into its store. Claude Code compactions produce implicit summaries. Should `claude-recall` extract these and index them as a separate tier? Open question.
3. **Per-project config overrides.** Currently config is user-global. A project-level `.claude-recall.toml` could override thresholds per-project. Useful? Deferred.
4. **Long-term archive rotation.** What happens at 10,000 sessions / 10M messages? FTS5 still works but full rebuild times matter. Defer until scale is real.
5. **Memory tier integration.** Claude Code's native auto-memory writes to `memory/MEMORY.md`. Should `claude-recall` surface its search results as memory entries the user can curate? Probably not — memory is curated, archive is raw. Keep separate. Document the distinction in INTEGRATION-GUIDE.md.

---

## 13. Relation to Microsoft's auto-memory

auto-memory (the prior-art Copilot CLI tool that inspired this project) and `claude-recall` share the same design pattern:

- Read-only query over a write layer the agent already maintains.
- Three-tier query interface (list, search, show).
- Instruction/hook integration that makes the query automatic.
- Token-efficient structured output.

`claude-recall` differs in:

- **Target agent.** Claude Code, not Copilot CLI.
- **Persistence format.** JSONL files parsed at index time, not SQLite written by the agent.
- **Integration mechanism.** Hooks with `additionalContext` injection, not `copilot-instructions.md` block.
- **Platform support.** Windows first-class; auto-memory is Linux/macOS-oriented.

The underlying insight is the same — when the agent already writes structured session data, adding a cheap query layer with automatic integration is an outsized productivity win. This project is the Claude Code instance of that insight.

---

*End of architecture doc.*
