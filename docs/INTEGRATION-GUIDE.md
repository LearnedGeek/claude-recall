# claude-recall — Integration Guide

How to wire `claude-recall` into any Claude Code project. Covers the generic flow and a worked example on the **ANI Runtime** project for reference.

---

## 1. Prerequisites

- Python 3.11 or newer
- Claude Code installed, with at least one prior session in the project's `.jsonl` archive
- Write access to the project's `.claude/` directory

Verify the archive exists for your project:

```bash
# Find your project's session archive
ls ~/.claude/projects/
```

You should see one directory per project, each containing `.jsonl` files. The directory name is a slug derived from the project's path. For a project at `E:\Documents\Work\dev\repos\AmbientNaturalIntelligence`, the slug is `e--Documents-Work-dev-repos-AmbientNaturalIntelligence`.

---

## 2. Install

```bash
# From PyPI (once v0.2 ships)
pip install claude-recall

# Or from source during development
cd ~/dev/repos/claude-recall
pip install -e .
```

Verify:

```bash
claude-recall --version
# 0.1.0
```

---

## 3. First-time index

From anywhere:

```bash
claude-recall index --verbose
```

This walks `~/.claude/projects/*/*.jsonl`, extracts messages, and writes them to `~/.config/claude-recall/index.db`. First run takes a few seconds per ~10k messages. Subsequent runs are incremental and nearly instant.

Confirm:

```bash
claude-recall status --format agent-context
# claude-recall: 87 sessions, 12,402 messages indexed, most recent 2026-04-22T20:47
```

(Plain `claude-recall status` prints the full multi-line health report with
archive/db paths, schema version, and per-check booleans.)

---

## 4. Wire the hooks into a project

`cd` to the project root, then:

```bash
claude-recall init-hooks
```

What this does:

1. Creates `.claude/hooks/` if it doesn't exist.
2. Copies the platform-appropriate hook scripts (`.sh` on Unix, `.ps1` on Windows) to `.claude/hooks/`.
3. Makes them executable (Unix).
4. Merges the hook registration into `.claude/settings.json`, backing up the original to `.claude/settings.json.bak`.

The resulting `settings.json` block:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "command": ".claude/hooks/claude-recall-session-start.sh"
      }
    ],
    "UserPromptSubmit": [
      {
        "command": ".claude/hooks/claude-recall-on-prompt.sh"
      }
    ]
  }
}
```

If you have existing hooks, they are preserved — `init-hooks` uses a merge, not a replace.

---

## 5. Verify it's working

### 5.1 SessionStart

Start a new Claude Code session in your project. You should see an injected status note near the top of the conversation context (often invisible to you but visible to Claude). Ask Claude:

> What does claude-recall say about my session archive?

Claude should respond with the status line it received from the hook.

### 5.2 UserPromptSubmit

Ask Claude something that references prior work:

> Remind me what we decided about regex patterns a few weeks ago.

If the archive has relevant matches above the configured threshold, Claude will receive prior-session context as `additionalContext` and reference it in the answer.

If nothing injects (no matches above threshold), the prompt flows normally — no error, no delay.

### 5.3 Manual query (sanity check)

From any directory:

```bash
claude-recall search "regex patterns" --days 30 --limit 5
```

You should see ranked matches with timestamps and snippets.

---

## 6. Configuration

Create `~/.config/claude-recall/config.toml` (or `%APPDATA%/claude-recall/config.toml` on Windows) to override defaults. Full schema in [PLAN.md §8](PLAN.md#8-configuration).

**Common tweaks:**

```toml
[search]
# Raise this if hook injections feel noisy. Lower it if you feel like
# relevant context is being dropped.
hook_threshold = 0.5

# Cap how much gets injected. Default 800 tokens.
max_injected_tokens = 600
```

---

## 7. Worked example: ANI Runtime project

The ANI Runtime project (`AmbientNaturalIntelligence`) is a case study for `claude-recall`'s design rationale. It has months of session history, frequent context compactions, and heavy reliance on prior-conversation context. Here is exactly how to wire it in.

### 7.1 Project setup

```bash
cd E:/Documents/Work/dev/repos/AmbientNaturalIntelligence
claude-recall init-hooks
```

This creates:
- `.claude/hooks/claude-recall-session-start.sh` (or `.ps1` on Windows)
- `.claude/hooks/claude-recall-on-prompt.sh` (or `.ps1`)
- Updates `.claude/settings.json`

### 7.2 Initial index

The ANI project has substantial session history. First index:

```bash
claude-recall index --project e--Documents-Work-dev-repos-AmbientNaturalIntelligence --verbose
```

Expected output on a recent archive:

```
[indexer] scanning ~/.claude/projects/e--Documents-Work-dev-repos-AmbientNaturalIntelligence
[indexer] session 7e420c4f-af6c-4f5f-af34-a351b90ee10d: 2,341 messages
[indexer] session 02cf9913-c9c2-46e8-8b49-91c71ba90b8e: 418 messages
[indexer] ... (truncated)
[indexer] done — 12 new, 0 updated, 0 unchanged, 8,421 messages total (3.4s)
```

### 7.3 Calibration

The ANI project already has a feedback memory at `memory/feedback_search_prior_conversations.md` that instructs Claude to grep the `.jsonl` archive when Mark references prior decisions. That rule is **complementary** to `claude-recall`, not redundant:

- The feedback memory is a **behavioral rule** — Claude should look at prior sessions.
- `claude-recall` is the **mechanism** — it gives Claude a cheap, ranked query path rather than ad-hoc grep.

Once `claude-recall` is wired in, the feedback memory can be updated to point at the CLI instead of raw grep. Suggested rewrite (for the ANI project's `memory/feedback_search_prior_conversations.md`):

> When Mark references a prior decision or established principle, query the session archive via `claude-recall search "<term>" --days 60`. The UserPromptSubmit hook will usually surface relevant context automatically; the explicit CLI query is a fallback when the hook didn't inject something you need.

### 7.4 Tuning for a long archive

ANI's archive is larger than typical. Reasonable tunings:

```toml
# ~/.config/claude-recall/config.toml
[search]
hook_threshold = 0.4     # slightly permissive; ANI references a lot of prior work
hook_limit = 5           # give the model a broader recall set
hook_days = 60           # ANI's active development spans months
max_injected_tokens = 1200  # ANI's context budget is already large; more room for recall
```

### 7.5 What Claude gains from this integration on ANI

When Mark says *"remind me why we removed the regex in Feature 14"*, the hook fires on submit, searches for "regex Feature 14", finds the April-21 research-log entry + the original design conversation, and injects the relevant snippets. Claude answers from the retrieved context rather than guessing or requiring Mark to paste the history.

When Mark says *"what was the conclusion on the Mistral A/B?"*, the hook finds the March-22 session where that was decided, along with the finding about epistemic-gap behavior divergence. Claude answers from that context.

When Mark says *"let's keep building the agentic lens design"*, SessionStart has already primed Claude with the status line, and UserPromptSubmit finds the April-22 design doc session. Claude picks up exactly where the prior session left off.

---

## 8. Multi-project use

`claude-recall` indexes all projects globally but scopes hook queries to the
current project. Since v0.2.1 the shipped `UserPromptSubmit` hook passes
`--project auto`, which resolves the current working directory to the
matching project slug (handling both lowercase and legacy uppercase slugs).

For direct CLI queries you have three options:

```bash
# Auto-scope to cwd (what the hook does):
claude-recall search "design decision" --project auto

# Explicit slug:
claude-recall search "design decision" --project e--Documents-Work-dev-repos-AmbientNaturalIntelligence

# Cross-project recall (insight from Project A informs Project B):
claude-recall search "design decision"          # no --project = global
```

v0.4 will add an explicit `--all-projects` flag; for now, omitting `--project`
is the cross-project path.

---

## 9. Common adjustments

| Symptom | Fix |
|---|---|
| "Too much irrelevant context is being injected" | Raise `hook_threshold` in config. Default `0.3` → try `0.5`. |
| "Relevant context is being missed" | Lower `hook_threshold`. Or add an explicit `claude-recall search` invocation in your message when you need assurance. |
| "The hook returns `{}` on most natural-language prompts" | Fixed in v0.2.0 via `--extract-keywords` in the shipped hook scripts. The hook strips stopwords/pronouns/fillers before FTS5 so topical tokens surface cleanly. v0.1.1 introduced an OR-join fallback as a stepping-stone; v0.2.0 is the proper fix. If you're still on v0.1.0 or v0.1.1, upgrade ([issue #1](https://github.com/LearnedGeek/claude-recall/issues/1)). |
| "Hook returns matches from the wrong project on a multi-project install" | Fixed in v0.2.1. The shipped hook now passes `--project auto`, which resolves to the current working directory's slug. Upgrade and run `claude-recall init-hooks --force` to pick up the new hook script ([issue #2](https://github.com/LearnedGeek/claude-recall/issues/2)). |
| "My `hook_days`/`hook_limit`/`hook_threshold` settings in `config.toml` aren't taking effect" | Fixed in v0.2.1. Pre-v0.2.1 hooks hardcoded `--days 30 --limit 3 --threshold 0.3`. The v0.2.1 hook passes `--from-config` so those values come from `[search]` in `config.toml`. Upgrade and run `claude-recall init-hooks --force`. |
| "I upgraded `claude-recall` and something feels off" | Run `claude-recall status`. Since v0.2.1 it reports `package_version` vs. `installed_hook_version` and flags stale hooks. Run `claude-recall init-hooks --force` to align. |
| "I want semantic retrieval in the hook, not just the CLI" | v0.3.0 ships with `[embeddings].use_in_hook = false` because semantic-on hook latency is ~700ms (over PLAN §7.3 budget). Once you've verified Ollama is warm and the 200ms overrun is acceptable, set `use_in_hook = true` in `~/.config/claude-recall/config.toml` (or `%APPDATA%\claude-recall\config.toml`). The v0.4.0 compiled hook binary removes the budget concern. |
| "How do I turn on embeddings?" | `pip install 'claude-recall[embeddings]'`, ensure Ollama is running and `ollama pull nomic-embed-text`, set `[embeddings].enabled = true` in config, then run `claude-recall embed --verbose` once. Check `claude-recall status --format agent-context` — it should show `Embeddings: <N> vectors, Ollama reachable`. |
| "SessionStart hook is slow" | Confirm index is incremental by checking `claude-recall status`; run `claude-recall index --rebuild` once to normalize. |
| "Hook fires but output never shows up in Claude" | Check `.claude/settings.json` was merged correctly. Run the hook manually — `bash .claude/hooks/claude-recall-on-prompt.sh` with a test JSON input — and confirm stdout is valid JSON. |
| "FTS5 isn't available on my system" | Rare. `claude-recall status` reports `fts_available: false`. Upgrade Python or rebuild with FTS5 support. |

---

## 10. Uninstall

```bash
# Remove hook registrations from settings.json
claude-recall uninstall-hooks        # v0.2+

# Or manually: delete .claude/hooks/claude-recall-*.{sh,ps1} and the hooks block from .claude/settings.json

# Remove the database and config
rm -rf ~/.config/claude-recall/

# Uninstall the package
pip uninstall claude-recall
```

None of the above touches the Claude Code archive itself.

---

## 11. Integration checklist

- [ ] `pip install claude-recall` (or dev install)
- [ ] `claude-recall --version` returns expected version
- [ ] `claude-recall index` completes successfully
- [ ] `claude-recall status` reports > 0 sessions
- [ ] `cd <project-root> && claude-recall init-hooks` succeeds
- [ ] `.claude/settings.json` contains hook registrations (and `.bak` was created)
- [ ] `.claude/hooks/claude-recall-*.sh` (or `.ps1`) exists and is executable
- [ ] New Claude Code session in that project shows evidence of status injection
- [ ] A prompt referencing prior work produces visible use of prior context
- [ ] `hook_threshold` tuned if recall feels off

Once all boxes check, `claude-recall` is integrated and working.

---

*End of integration guide.*
