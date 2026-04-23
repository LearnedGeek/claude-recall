# claude-recall

Automatic, cheap, precise query of your Claude Code session archive — wired into the prompt flow via hooks so you never have to remember to search.

**Status:** Design + scaffolding. MVP implementation pending. See [docs/PLAN.md](docs/PLAN.md) for the full implementation plan.

---

## What it does

Claude Code writes every session to `~/.claude/projects/<project-slug>/<session-id>.jsonl` — raw turn-by-turn log. That archive is already a goldmine; the problem is that after context compaction, Claude loses mid-session detail, and no native Claude Code feature surfaces prior-session context back into the active conversation. Manual grep works but rarely happens.

`claude-recall` closes that gap:

1. **Indexes** the `.jsonl` archive into a local SQLite database with FTS5 full-text search. Incremental, read-only against the archive.
2. **Queries** via CLI — ranked matches with snippets, sub-millisecond over hundreds of thousands of messages.
3. **Injects** relevant prior-session context into the active Claude Code session automatically via `SessionStart` and `UserPromptSubmit` hooks. You don't have to remember to look.

---

## Install (placeholder — not yet published)

```bash
pip install claude-recall
claude-recall init-hooks        # wires hooks into the current project's .claude/settings.json
claude-recall index             # first-time index of the session archive
```

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

## Documentation

- [**docs/PLAN.md**](docs/PLAN.md) — full implementation plan, specifications, acceptance criteria. Self-contained so any competent agent or engineer can pick it up and execute.
- [**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md) — deeper architectural design, rationale, alternatives considered.
- [**docs/INTEGRATION-GUIDE.md**](docs/INTEGRATION-GUIDE.md) — how to wire `claude-recall` into any Claude Code project, with a worked example on the ANI Runtime project.
- [**docs/BLOG-POST.md**](docs/BLOG-POST.md) — draft outline for the launch blog post.
- [**docs/LINKEDIN-POST.md**](docs/LINKEDIN-POST.md) — draft outline for the LinkedIn announcement.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Author

Mark McArthey / [Learned Geek](https://learnedgeek.com)
