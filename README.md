# claude-recall

Automatic, cheap, precise query of your Claude Code session archive — wired into the prompt flow via hooks so you never have to remember to search.

> ### Status: beta
>
> **claude-recall is in daily production use on the maintainer's workflow against a 25,000-message session archive.** The API and hook contract are stable. Known issues are tracked in GitHub (see [Known Issues](#known-issues)).
>
> **Install via PyPI:** `pip install 'claude-recall[embeddings]'`. Beta = rough edges get filed, triaged, and shipped quickly (nine issues fixed across the dogfooding cycle so far — see the changelog).
>
> Feedback welcome via GitHub issues. The tool handles single-user, single-machine session archives. Multi-user / shared-recall is explicitly out of scope for beta.

**What's landed through v0.5.x:** MVP + keyword extraction + auto project scoping + config-driven hook flags + stale-hook detection + opt-in semantic retrieval via Ollama + NativeAOT C# hook binary + `init-hooks --force` overwrite semantics + PyPI publish. See [the original plan](https://github.com/LearnedGeek/claude-recall/blob/main/docs/PLAN.md), [feature plans](https://github.com/LearnedGeek/claude-recall/blob/main/docs/EMBEDDINGS-PLAN.md), and the [changelog](https://github.com/LearnedGeek/claude-recall/blob/main/CHANGELOG.md) for the full history.

---

## What it does

Claude Code writes every session to `~/.claude/projects/<project-slug>/<session-id>.jsonl` — raw turn-by-turn log. That archive is already a goldmine; the problem is that after context compaction, Claude loses mid-session detail, and no native Claude Code feature surfaces prior-session context back into the active conversation. Manual grep works but rarely happens.

`claude-recall` closes that gap:

1. **Indexes** the `.jsonl` archive into a local SQLite database with FTS5 full-text search. Incremental, read-only against the archive.
2. **Queries** via CLI — ranked matches with snippets, sub-millisecond FTS5 lookup over hundreds of thousands of messages.
3. **Injects** relevant prior-session context into the active Claude Code session automatically via `SessionStart` and `UserPromptSubmit` hooks. You don't have to remember to look.

---

## Install

```bash
pip install 'claude-recall[embeddings]'
claude-recall init-hooks
claude-recall index
```

Pip picks the right wheel for your platform:

- **Windows x64** gets the NativeAOT hook binary bundled automatically (fast-path hook)
- **macOS / Linux** get the pure-Python shell-hook fallback

To turn on semantic rerank (optional but recommended):

```bash
ollama pull nomic-embed-text
# Edit ~/.config/claude-recall/config.toml (or %APPDATA%\claude-recall\config.toml):
#   [embeddings]
#   enabled = true
claude-recall embed --probe    # sanity-check Ollama path
claude-recall embed --verbose  # one-time corpus embed
```

The `init-hooks` output also points at the section of the INTEGRATION-GUIDE that walks through this.

> **Don't use `pip install git+https://...`** — that builds from source and the NativeAOT hook binary is CI-built package data, not part of the source tree. A `git+https` install silently gives you the slower Python hook path on every platform.

---

## Quickstart query

```bash
claude-recall search "regex patterns" --days 30 --limit 5
claude-recall search "something you roughly remember" --semantic    # requires embeddings enabled
claude-recall show <session-id>
claude-recall status
```

---

## Architecture

```
┌─────────────────────────────────────┐
│  ~/.claude/projects/<project>/*.jsonl│
│  (Claude Code's raw session archive) │
└────────────┬────────────────────────┘
             │ read-only
             ▼
┌─────────────────────────────────────┐
│  claude-recall indexer               │
│  walks jsonl, extracts message pairs │
└────────────┬────────────────────────┘
             │ writes
             ▼
┌─────────────────────────────────────┐
│  SQLite DB with FTS5 virtual table   │
│  ~/.config/claude-recall/index.db    │
└────────────┬────────────────────────┘
             │ queried by
             ▼
┌──────────────────┐  ┌─────────────────────┐
│  CLI (manual)    │  │  Hooks (automatic)  │
│  recall search   │  │  SessionStart       │
│  recall show     │  │  UserPromptSubmit   │
└──────────────────┘  └─────────┬───────────┘
                                │ additionalContext
                                ▼
                      ┌─────────────────┐
                      │  Claude Code    │
                      │  active session │
                      └─────────────────┘
```

---

## Hook latency expectations

Concrete numbers from dogfooding so you can calibrate. Latency scales with archive size:

| Archive size | Hook path | Typical latency |
|---|---|---|
| Small (<1k messages) | FTS5 only | ~15ms |
| Small (<1k messages) | Semantic rerank + warm Ollama | ~80ms |
| Large (~25k messages) | FTS5 only | ~100-200ms |
| Large (~25k messages) | Semantic rerank + warm Ollama | ~2-5s warm, ~10s cold-model |

The cold-model case is Ollama unloading `nomic-embed-text` between invocations (default `keep_alive=5m`). The first prompt after idle pays the model-load cost; subsequent prompts land on the warm model. If latency bothers you, tune `[embeddings].request_timeout_seconds` and consider keeping Ollama warm via another process.

If the semantic hook feels slow on your archive size, flip `[embeddings].use_in_hook = false` to run FTS5-only. You still get the `--semantic` flag for manual `claude-recall search` invocations where extra latency is acceptable.

---

## Why this exists

Claude Code has excellent primitives — the `.jsonl` session archive, hooks with `additionalContext` injection, skills, auto-memory markdown files. It does not have native session-archive search as of April 2026. `claude-recall` fills that gap with a small, self-contained Python CLI plus two hook scripts, following the pattern established by Microsoft's [auto-memory](https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/) project for Copilot CLI — adapted for Claude Code's persistence model.

---

## Known Issues

Live list of issues caught in beta use. Filing new issues is explicitly welcome — the beta is the feedback channel.

- **Open issues:** see [github.com/LearnedGeek/claude-recall/issues](https://github.com/LearnedGeek/claude-recall/issues) for the current list.
- **Recently closed (not a full changelog — see the [CHANGELOG](https://github.com/LearnedGeek/claude-recall/blob/main/CHANGELOG.md) for that):**
  - [#8](https://github.com/LearnedGeek/claude-recall/issues/8) — `embed` batches used to fail the whole 32-message batch when one input was oversized, silently dropping 27% of vectors. Fixed in v0.4.3 with per-input truncation, error-body surfacing, and per-message singleton fallback. Dogfooding validated 99.95% coverage on a 25k-message archive after the fix.
  - [#4](https://github.com/LearnedGeek/claude-recall/issues/4) — `init-hooks --force` merged with existing settings.json instead of overwriting, producing duplicate `UserPromptSubmit` entries when upgrading from a manual wiring. Fixed in v0.4.2.
  - [#3](https://github.com/LearnedGeek/claude-recall/issues/3) — `init-hooks --force` crashed on Windows native-binary wheels (missing shell-script sources in the wheel). Fixed in v0.4.1.
  - [#2](https://github.com/LearnedGeek/claude-recall/issues/2) — Hook didn't auto-scope to current project + hardcoded `--days 30` ignored config. Fixed in v0.2.1.
  - [#1](https://github.com/LearnedGeek/claude-recall/issues/1) — `UserPromptSubmit` hook returned empty on natural-language prompts. Fixed via stdlib keyword extraction in v0.2.0.

**Beta expectations:** I find issues by using the tool. You may find different issues by using it differently. That's the point of a beta. File and describe — triage is fast.

---

## Documentation

- [**PLAN.md**](https://github.com/LearnedGeek/claude-recall/blob/main/docs/PLAN.md) — full implementation plan, specifications, acceptance criteria.
- [**ARCHITECTURE.md**](https://github.com/LearnedGeek/claude-recall/blob/main/docs/ARCHITECTURE.md) — deeper architectural design, rationale, alternatives considered.
- [**INTEGRATION-GUIDE.md**](https://github.com/LearnedGeek/claude-recall/blob/main/docs/INTEGRATION-GUIDE.md) — how to wire `claude-recall` into any Claude Code project.
- [**EMBEDDINGS-PLAN.md**](https://github.com/LearnedGeek/claude-recall/blob/main/docs/EMBEDDINGS-PLAN.md) — v0.3 semantic-retrieval feature plan.
- [**HOOK-BINARY-PLAN.md**](https://github.com/LearnedGeek/claude-recall/blob/main/docs/HOOK-BINARY-PLAN.md) — v0.4 NativeAOT C# hook binary plan.
- [**CHANGELOG.md**](https://github.com/LearnedGeek/claude-recall/blob/main/CHANGELOG.md) — full release history.

---

## License

MIT. See [LICENSE](https://github.com/LearnedGeek/claude-recall/blob/main/LICENSE).

---

## Author

Mark McArthey / [Learned Geek](https://learnedgeek.com)
