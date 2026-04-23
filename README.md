# claude-recall

Automatic, cheap, precise query of your Claude Code session archive — wired into the prompt flow via hooks so you never have to remember to search.

> ### Status: beta
>
> **v0.4.2 is in daily production use on the maintainer's workflow against a 25,000-message session archive. The API and hook contract are stable. Known issues are tracked in GitHub (see [Known Issues](#known-issues)).**
>
> **v0.5 will publish to PyPI** so the install story becomes `pip install claude-recall` without a release-wheel URL. Until then, install from a tagged GitHub Release wheel (instructions below).
>
> Feedback welcome via GitHub issues. The tool handles single-user, single-machine session archives. Multi-user / shared-recall is explicitly out of scope for beta.

**What landed at v0.4.2:** MVP + keyword extraction + auto project scoping + config-driven hook flags + stale-hook detection + opt-in semantic retrieval via Ollama + **NativeAOT C# hook binary for sub-100ms semantic-enabled hooks on Windows** + `init-hooks --force` overwrite semantics for clean reinstalls. See [docs/PLAN.md](docs/PLAN.md) for the original plan, [docs/EMBEDDINGS-PLAN.md](docs/EMBEDDINGS-PLAN.md) / [docs/HOOK-BINARY-PLAN.md](docs/HOOK-BINARY-PLAN.md) for feature plans, and [CHANGELOG.md](CHANGELOG.md) for what's landed.

---

## What it does

Claude Code writes every session to `~/.claude/projects/<project-slug>/<session-id>.jsonl` — raw turn-by-turn log. That archive is already a goldmine; the problem is that after context compaction, Claude loses mid-session detail, and no native Claude Code feature surfaces prior-session context back into the active conversation. Manual grep works but rarely happens.

`claude-recall` closes that gap:

1. **Indexes** the `.jsonl` archive into a local SQLite database with FTS5 full-text search. Incremental, read-only against the archive.
2. **Queries** via CLI — ranked matches with snippets, sub-millisecond over hundreds of thousands of messages.
3. **Injects** relevant prior-session context into the active Claude Code session automatically via `SessionStart` and `UserPromptSubmit` hooks. You don't have to remember to look.

---

## Install

Not on PyPI yet — v0.5 target. Until then, install from a GitHub Release wheel.

**Windows x64** (gets the NativeAOT hook binary bundled for ~80ms semantic hooks):

```bash
pip install --upgrade "claude_recall[embeddings] @ https://github.com/LearnedGeek/claude-recall/releases/download/v0.4.2/claude_recall-0.4.2-py3-none-win_amd64.whl"
claude-recall init-hooks
claude-recall index
```

**macOS / Linux** (pure-Python wheel — shell-hook fallback path, no compiled binary yet):

```bash
pip install --upgrade "claude_recall[embeddings] @ https://github.com/LearnedGeek/claude-recall/releases/download/v0.4.2/claude_recall-0.4.2-py3-none-any.whl"
claude-recall init-hooks
claude-recall index
```

> **Don't use `pip install git+https://...`** — that builds a wheel from the source tree which does not include the NativeAOT binary (it's CI-built per-platform). The `git+https` install works but gives you the pure-Python hook path on every platform.

---

## Quickstart query

```bash
claude-recall search "regex patterns" --days 30 --limit 5
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

## Why this exists

Claude Code has excellent primitives — the `.jsonl` session archive, hooks with `additionalContext` injection, skills, auto-memory markdown files. It does not have native session-archive search as of April 2026. `claude-recall` fills that gap with a small, self-contained Python CLI plus two hook scripts, following the pattern established by Microsoft's [auto-memory](https://devblogs.microsoft.com/all-things-azure/i-wasted-68-minutes-a-day-re-explaining-my-code-then-i-built-auto-memory/) project for Copilot CLI — adapted for Claude Code's persistence model.

---

## Known Issues

Live list of issues caught in beta use. Filing new issues is explicitly welcome — the beta is the feedback channel.

- **Open issues:** see [github.com/LearnedGeek/claude-recall/issues](https://github.com/LearnedGeek/claude-recall/issues) for the current list.
- **Recently closed (not a full changelog — see [CHANGELOG.md](CHANGELOG.md) for that):**
  - [#4](https://github.com/LearnedGeek/claude-recall/issues/4) — `init-hooks --force` merged with existing settings.json instead of overwriting, producing duplicate `UserPromptSubmit` entries when upgrading from a manual wiring. Fixed in v0.4.2 — `--force` now rewrites managed hook events cleanly; non-force merge behavior preserved.
  - [#3](https://github.com/LearnedGeek/claude-recall/issues/3) — `init-hooks --force` crashed on Windows native-binary wheels (missing shell-script sources in the wheel). Fixed in v0.4.1.
  - [#2](https://github.com/LearnedGeek/claude-recall/issues/2) — Hook didn't auto-scope to current project + hardcoded `--days 30` ignored config. Fixed in v0.2.1.
  - [#1](https://github.com/LearnedGeek/claude-recall/issues/1) — `UserPromptSubmit` hook returned empty on natural-language prompts. Fixed via stdlib keyword extraction in v0.2.0.

**Beta expectations:** I find issues by using the tool. You may find different issues by using it differently. That's the point of a beta. File and describe — I'll triage quickly.

---

## Documentation

- [**docs/PLAN.md**](docs/PLAN.md) — full implementation plan, specifications, acceptance criteria. Self-contained so any competent agent or engineer can pick it up and execute.
- [**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md) — deeper architectural design, rationale, alternatives considered.
- [**docs/INTEGRATION-GUIDE.md**](docs/INTEGRATION-GUIDE.md) — how to wire `claude-recall` into any Claude Code project, with a worked example on the ANI Runtime project.
- [**docs/BLOG-POST.md**](docs/BLOG-POST.md) — outline for the launch blog post (kept as design artifact).
- [**docs/BLOG-POST-DRAFT.md**](docs/BLOG-POST-DRAFT.md) — full draft of the launch post in the maintainer's voice (pending author rewrite pass).
- [**docs/LINKEDIN-POST.md**](docs/LINKEDIN-POST.md) — LinkedIn announcement drafts (short-form).

---

## License

MIT. See [LICENSE](LICENSE).

---

## Author

Mark McArthey / [Learned Geek](https://learnedgeek.com)
