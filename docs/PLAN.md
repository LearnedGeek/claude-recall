# claude-recall — Implementation Plan

**Audience:** The agent or engineer picking up this repo to build the MVP.
**Status:** Design complete. Scaffolding in place. Implementation has not started.
**Last updated:** April 23, 2026

This document is self-contained. Do not assume prior conversation context. Everything needed to execute the MVP is here.

---

## 1. Intent

`claude-recall` is a small Python CLI plus two Claude Code hook scripts. It solves one problem: **intra-session context compaction in Claude Code is lossy, and the raw session archive that preserves the full detail is not queryable cheaply or automatically.**

The tool indexes the `.jsonl` session archive Claude Code already writes, exposes cheap query via SQLite FTS5, and injects relevant prior-session context into the active session via hooks so the user does not have to remember to search.

**Three properties the MVP must satisfy:**

1. **Not fragile.** Structured SQLite store, schema-enforced. Not markdown files. Not shell-glued grep.
2. **Precise and responsive.** FTS5 ranked results in sub-millisecond. Optional embedding layer for semantic retrieval.
3. **Automatic.** Two hooks that fire without user intervention — SessionStart to prime the session with recent context, UserPromptSubmit to inject prior matches when the user's prompt references prior work.

---

## 2. Non-goals (explicit)

These are **out of scope for the MVP** and should not creep in:

- **Not a replacement for CLAUDE.md or the auto-memory markdown system.** Those store curated, typed facts. This tool stores raw message-pair queries over the session archive. Different purpose, different store.
- **Not a log viewer.** No TUI, no web UI. CLI and hook output only.
- **Not multi-user / server-side.** Single-user local tool.
- **Not cloud-synced.** All data stays in `~/.config/claude-recall/`.
- **Does not modify `.jsonl` archive.** Strictly read-only.
- **Not opinionated about what to save.** Indexes everything; filter at query time.
- **Not an LLM proxy.** Does not call Ollama, Claude API, or any model in MVP. The optional embedding layer (post-MVP) is the only LLM touch point and is opt-in.

---

## 3. Architecture

```
┌────────────────────────────────────────────────────┐
│  ~/.claude/projects/<project-slug>/<uuid>.jsonl     │
│  (Claude Code's raw session archive — read-only)    │
└──────────────────────┬─────────────────────────────┘
                       │ walked by
                       ▼
┌────────────────────────────────────────────────────┐
│  claude_recall.indexer                              │
│  - parses JSONL line-by-line                        │
│  - extracts (role, content, timestamp, session_id)  │
│  - incremental by file mtime                        │
└──────────────────────┬─────────────────────────────┘
                       │ writes to
                       ▼
┌────────────────────────────────────────────────────┐
│  SQLite DB at ~/.config/claude-recall/index.db      │
│  - sessions table (metadata)                        │
│  - messages table (content)                         │
│  - messages_fts (FTS5 virtual, content indexed)     │
└───────┬───────────────────────────────┬────────────┘
        │ queried by                    │ queried by
        ▼                               ▼
┌────────────────────┐    ┌────────────────────────────┐
│ claude_recall.cli  │    │  Hooks (project-level)      │
│ - index            │    │  .claude/hooks/             │
│ - search           │    │   claude-recall-*.sh        │
│ - show             │    └────────┬───────────────────┘
│ - list             │             │ output JSON with
│ - status           │             │ `additionalContext`
│ - init-hooks       │             ▼
└────────────────────┘    ┌────────────────────────────┐
                          │  Claude Code active session │
                          │  (hook output auto-injected)│
                          └────────────────────────────┘
```

---

## 4. Tech stack

| Component | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | stdlib coverage, pattern-matching, f-strings, Path, good typing |
| Storage | SQLite (stdlib `sqlite3`) | Zero deps, FTS5 included, sub-ms queries, ACID |
| Search | FTS5 virtual table + BM25 ranking | Built into SQLite stdlib, proven at scale |
| CLI | `argparse` (stdlib) | Zero deps |
| Config | `~/.config/claude-recall/config.toml` (`tomllib` stdlib) | Standard location, zero deps |
| JSONL parsing | `json` stdlib | No external parser needed |
| Packaging | `setuptools` + `pyproject.toml` | Modern standard |
| Testing | `pytest` | De facto standard; dev-dep only |
| Embeddings (optional post-MVP) | Ollama `nomic-embed-text` via `httpx` | Already running on most Claude Code users' machines; matches ANI stack |

**Zero external runtime dependencies for MVP.** Optional `[embeddings]` extra adds `numpy` and `httpx` only when the user opts in.

---

## 5. Data model

### 5.1 Full DDL

```sql
-- Session metadata. One row per .jsonl file parsed.
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,            -- UUID from filename
    project_slug     TEXT NOT NULL,               -- parent dir name, e.g. "e--Documents-Work-dev-repos-AmbientNaturalIntelligence"
    file_path        TEXT NOT NULL UNIQUE,        -- absolute path to .jsonl
    file_mtime       REAL NOT NULL,               -- for incremental re-index
    started_at       TEXT,                        -- ISO8601 from first message ts
    ended_at         TEXT,                        -- ISO8601 from last message ts
    turn_count       INTEGER NOT NULL DEFAULT 0,  -- messages in session
    indexed_at       TEXT NOT NULL                -- when we last parsed this file
);

CREATE INDEX IF NOT EXISTS idx_sessions_project_started
    ON sessions(project_slug, started_at DESC);

-- Individual messages. One row per user or assistant turn.
CREATE TABLE IF NOT EXISTS messages (
    msg_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,                -- 'user' | 'assistant' | 'system' | 'tool'
    content         TEXT NOT NULL,
    turn_index      INTEGER NOT NULL,             -- order within session
    timestamp       TEXT,                         -- ISO8601 if available on the JSONL line
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_turn
    ON messages(session_id, turn_index);

CREATE INDEX IF NOT EXISTS idx_messages_timestamp
    ON messages(timestamp DESC);

-- Full-text search virtual table. Content-linked to messages.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='msg_id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync with messages.
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.msg_id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.msg_id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.msg_id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.msg_id, new.content);
END;

-- Schema version for future migrations.
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version VALUES (1);
```

### 5.2 Schema notes

- **Content-addressed FTS** via `content='messages'` + `content_rowid='msg_id'` means FTS does not duplicate content storage; it points at the `messages` table. Saves disk.
- **Porter unicode61 tokenizer** handles English stemming and Unicode normalization. Good default. Swap to `trigram` if sub-word matching is needed later.
- **Schema version table** is forward-looking. MVP is v1. Migrations handled in `storage.py` via `PRAGMA user_version` reads + conditional ALTER.
- **Cascading delete on sessions** ensures re-indexing a file cleanly replaces its messages.

### 5.3 JSONL parse contract

Claude Code's `.jsonl` format is not formally documented. Based on observation, each line is a JSON object. The indexer must handle this contract **defensively** — unexpected fields are tolerated, malformed lines are logged and skipped, the indexer never crashes.

**Minimum fields the indexer will attempt to extract:**
- `type` or `role` — maps to `messages.role`
- `message.content` (string or array of content blocks) — flattened to text for `messages.content`
- `timestamp` or `message.timestamp` — ISO8601 if present, else NULL
- `sessionId` — if present on the line, else derive from filename

**Fallback extraction rules (when the above aren't cleanly present):**
- If `content` is a list of blocks: concatenate `text` fields with `\n`, skip tool-use / tool-result blocks (or store them as role=`tool` — see §5.4).
- If `role` missing: default to `unknown`; do not skip.
- If a line fails to parse as JSON: log warning, skip.

### 5.4 What to index vs. what to skip

**Index:**
- User messages (role=`user`)
- Assistant messages (role=`assistant`), textual content
- System messages (role=`system`) if they contain project-relevant context (not Claude Code's own boilerplate)

**Skip:**
- Tool-use invocation blocks (noisy, schematic)
- Tool-result blocks (often large, often binary-like)
- Purely whitespace content

**Configurable:** `config.toml` flag `index_tool_blocks: bool = false` lets users opt into indexing tool interactions if they want.

---

## 6. CLI specification

All commands exit with code 0 on success, non-zero on error. All commands accept `--help`. Machine-readable output via `--format json` where applicable.

### 6.1 `claude-recall index`

**Purpose:** Walk the archive, index new or changed files.

**Usage:**
```
claude-recall index [--project <slug>] [--archive-root <path>] [--rebuild] [--verbose]
```

**Options:**
- `--project <slug>` — limit to one project directory. Default: all projects.
- `--archive-root <path>` — override the archive root. Default: `~/.claude/projects`.
- `--rebuild` — drop all data for the targeted scope and re-index from scratch.
- `--verbose` — log per-file progress.

**Behavior:**
1. Glob `<archive_root>/<project_slug>/*.jsonl` (or all projects).
2. For each file, compare `mtime` to `sessions.file_mtime`. Skip if unchanged.
3. For changed files: DELETE existing messages for that session, re-parse, re-insert.
4. Print summary: `indexed N new, M updated, K unchanged. Total Z messages.`

**Exit codes:**
- 0: success
- 1: archive root does not exist
- 2: database error

**Example:**
```
$ claude-recall index --verbose
[indexer] scanning ~/.claude/projects (3 projects)
[indexer] AmbientNaturalIntelligence: 7 sessions, 6 new, 1 updated
[indexer] claude-recall: 0 sessions
[indexer] done — 6 new, 1 updated, 12 unchanged, 4,281 messages total (2.1s)
```

---

### 6.2 `claude-recall search`

**Purpose:** Query the index.

**Usage:**
```
claude-recall search <query> [--days N] [--limit N] [--project <slug>] [--threshold F] [--format json|text] [--agent-context]
```

**Options:**
- `<query>` — FTS5 query string (supports `AND`, `OR`, `NOT`, `*`, `"quoted phrases"`)
- `--days N` — only sessions started in last N days. Default: 90.
- `--limit N` — max results. Default: 10.
- `--project <slug>` — scope to one project. Default: all.
- `--threshold F` — BM25 score threshold; results below are dropped. Default: 0.0 (no threshold).
- `--format json|text` — output format. Default: `text`.
- `--agent-context` — output structured block suitable for injection as `additionalContext`. Implies `--format json` + result limit + trimming.

**Behavior:**
1. Parse query. Validate FTS5 syntax. If invalid, attempt quoted-phrase fallback.
2. Execute FTS5 query with BM25 ranking. Join to messages + sessions.
3. For each match, build a snippet (±50 characters around match, max 3 snippets per result).
4. Filter by `--days` and `--project` if specified.
5. Apply threshold.
6. Output ranked results.

**Output schema (`--format json`):**
```json
{
  "query": "regex patterns",
  "total_matches": 12,
  "returned": 5,
  "threshold": 0.0,
  "results": [
    {
      "session_id": "7e420c4f-af6c-4f5f-af34-a351b90ee10d",
      "project_slug": "e--Documents-Work-dev-repos-AmbientNaturalIntelligence",
      "started_at": "2026-04-21T18:32:00Z",
      "turn_index": 127,
      "role": "user",
      "score": 8.42,
      "snippet": "...we'd never use <mark>regex patterns</mark> for that because...",
      "content_preview": "we'd never use regex patterns for that because they are fragile..."
    }
  ]
}
```

**Output schema (`--agent-context`):** same JSON, but stripped of session-internal metadata and capped at `max_injected_tokens` from config.

**Exit codes:**
- 0: success (even if no matches)
- 1: database not initialized (suggest `claude-recall index`)
- 2: invalid FTS5 query
- 3: database error

---

### 6.3 `claude-recall show`

**Purpose:** Fetch a single session's full transcript.

**Usage:**
```
claude-recall show <session_id> [--format json|text] [--turns FROM-TO]
```

**Options:**
- `<session_id>` — UUID of the session.
- `--format json|text` — output format. Default: `text`.
- `--turns FROM-TO` — turn range, e.g. `0-20` or `-10` (last 10).

**Behavior:**
1. Look up session by ID.
2. Fetch messages ordered by turn_index.
3. Render.

**Exit codes:**
- 0: success
- 1: session not found

---

### 6.4 `claude-recall list`

**Purpose:** Enumerate recent sessions.

**Usage:**
```
claude-recall list [--project <slug>] [--days N] [--limit N] [--format json|text]
```

**Behavior:** Return sessions ordered by `started_at DESC` with summary info (turn count, date range, first user message snippet).

---

### 6.5 `claude-recall status`

**Purpose:** Health check. Used by SessionStart hook to report index state.

**Usage:**
```
claude-recall status [--format json|text|agent-context]
```

**Output:**
- Archive root (resolved)
- Database path + size
- Schema version
- Total sessions indexed
- Total messages indexed
- Most recent session timestamp
- Last index run timestamp
- Health checks:
  - `archive_accessible` (can read archive root)
  - `db_accessible` (can read/write db)
  - `schema_current` (schema version matches code expectation)
  - `fts_available` (FTS5 available in this SQLite build)

**`--agent-context`** format is a one-line summary like:
```
claude-recall: 87 sessions, 12,402 messages indexed, most recent 2026-04-22T20:47Z
```

---

### 6.6 `claude-recall init-hooks`

**Purpose:** One-command wiring into a project's `.claude/settings.json`.

**Usage:**
```
claude-recall init-hooks [--project-root <path>] [--force]
```

**Behavior:**
1. Resolve `.claude/` directory at `<project-root>` (default: cwd).
2. Create `.claude/hooks/` if missing.
3. Copy bundled hook scripts to `.claude/hooks/` (`claude-recall-session-start.sh` and `claude-recall-on-prompt.sh`). Make executable.
4. Read or create `.claude/settings.json`. Merge in the hooks block (see §7). Preserve any existing hooks.
5. Print confirmation with the next manual step ("run `claude-recall index` once to build the index").

**Safety:**
- Never overwrite existing hook files unless `--force` is passed.
- Back up `settings.json` to `settings.json.bak` before modifying.
- Validate JSON before writing.

---

## 7. Hook specifications

Two hooks, both as shell scripts that invoke the CLI and emit JSON on stdout.

### 7.1 SessionStart hook

**File:** `.claude/hooks/claude-recall-session-start.sh`
**Trigger:** Claude Code session start (`startup` and `resume` events).

**Behavior:**
1. Run `claude-recall index` incrementally (quiet).
2. Run `claude-recall status --format agent-context`.
3. Emit the status line as `additionalContext`.

**Body (cross-platform — bash for macOS/Linux; an equivalent `.ps1` provided for Windows):**

```bash
#!/usr/bin/env bash
# claude-recall SessionStart hook
set -e

# Silent incremental index. Errors do not block session start.
claude-recall index >/dev/null 2>&1 || true

# Status line as additionalContext.
STATUS=$(claude-recall status --format agent-context 2>/dev/null || echo "claude-recall: unavailable")

cat <<EOF
{
  "additionalContext": "claude-recall status: $STATUS\n\nPrior-session archive is searchable via the \`claude-recall search <query>\` CLI, and matching context will be auto-injected when your prompt references prior work."
}
EOF
```

**Windows PowerShell equivalent** at `claude-recall-session-start.ps1`:

```powershell
# claude-recall SessionStart hook (Windows)
$ErrorActionPreference = 'SilentlyContinue'

& claude-recall index 2>$null | Out-Null
$status = & claude-recall status --format agent-context 2>$null
if (-not $status) { $status = "claude-recall: unavailable" }

$out = @{
    additionalContext = "claude-recall status: $status`n`nPrior-session archive is searchable via the ``claude-recall search <query>`` CLI, and matching context will be auto-injected when your prompt references prior work."
}
$out | ConvertTo-Json -Compress
```

**Settings.json registration:**

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "command": ".claude/hooks/claude-recall-session-start.sh"
      }
    ]
  }
}
```

### 7.2 UserPromptSubmit hook

**File:** `.claude/hooks/claude-recall-on-prompt.sh`
**Trigger:** Claude Code `UserPromptSubmit` event — before each user message reaches the model.

**Behavior:**
1. Read the user's prompt from the hook input (Claude Code passes the prompt text on stdin as JSON).
2. Extract significant keywords (nouns, identifiers, quoted phrases).
3. Run `claude-recall search` with those keywords, relevance-thresholded, limit 3.
4. If any results cross threshold, emit them as `additionalContext`.
5. If no results: emit empty output (no injection).
6. Failsafe: on any error, emit empty output. **Never block the prompt.**

**Input contract (from Claude Code):**
```json
{
  "prompt": "Remind me what we decided about regex patterns a few days ago"
}
```

**Output contract (to Claude Code):**
```json
{
  "additionalContext": "Relevant prior-session context (claude-recall):\n\n[Session 7e420c4f, Apr 21] user: we'd never use regex patterns for that because they are fragile...\n\n[Session 7e420c4f, Apr 21] assistant: Agreed — architecture-over-instruction..."
}
```

**Body:**

```bash
#!/usr/bin/env bash
# claude-recall UserPromptSubmit hook
# On ANY failure, emit empty JSON and exit 0 — never block user prompts.

set +e  # don't exit on error
exec 2>/dev/null  # silence stderr

# Read prompt from stdin JSON
PROMPT=$(python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get('prompt', ''))
except Exception:
    print('')
" < /dev/stdin)

if [ -z "$PROMPT" ]; then
  echo '{}'
  exit 0
fi

# Run search with the full prompt as query. FTS5 is reasonably robust to natural
# language; explicit keyword extraction can come in v0.2.
RESULT=$(claude-recall search "$PROMPT" --days 30 --limit 3 --threshold 0.3 --agent-context 2>/dev/null)

if [ -z "$RESULT" ] || [ "$RESULT" = '{}' ]; then
  echo '{}'
  exit 0
fi

echo "$RESULT"
exit 0
```

**Windows equivalent** at `claude-recall-on-prompt.ps1`:

```powershell
# claude-recall UserPromptSubmit hook (Windows)
$ErrorActionPreference = 'SilentlyContinue'

try {
    $input_json = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $prompt = $input_json.prompt
    if (-not $prompt) { Write-Output '{}'; exit 0 }

    $result = & claude-recall search $prompt --days 30 --limit 3 --threshold 0.3 --agent-context 2>$null

    if (-not $result -or $result -eq '{}') {
        Write-Output '{}'
    } else {
        Write-Output $result
    }
} catch {
    Write-Output '{}'
}
exit 0
```

**Settings.json registration:**

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "command": ".claude/hooks/claude-recall-on-prompt.sh"
      }
    ]
  }
}
```

### 7.3 Hook performance budget

| Hook | Budget | Why |
|---|---|---|
| SessionStart | ≤ 2 seconds | Runs once per session start; user tolerance is moderate |
| UserPromptSubmit | ≤ 500ms | Runs on every prompt; anything slower is perceptible latency |

If UserPromptSubmit exceeds budget consistently, the threshold/limit flags should be tightened or the hook should be removed. The CLI itself must be <100ms for a typical search.

---

## 8. Configuration

**Location:** `~/.config/claude-recall/config.toml` (XDG-respecting on macOS/Linux; `%APPDATA%/claude-recall/config.toml` on Windows).

**Full schema:**

```toml
# claude-recall configuration

[archive]
# Root directory where Claude Code writes session .jsonl files.
root = "~/.claude/projects"

[database]
# Where the index lives. Any path is valid.
path = "~/.config/claude-recall/index.db"

[search]
# Default relevance threshold for hook-triggered searches.
# Higher = fewer, more-relevant injections.
hook_threshold = 0.3
# Max results a hook injection may contain.
hook_limit = 3
# Max tokens of additionalContext a hook may inject (soft limit).
max_injected_tokens = 800
# Default `--days` window for hook searches.
hook_days = 30

[indexing]
# Whether to index tool_use / tool_result message blocks.
# Default false because they are noisy and large.
index_tool_blocks = false

[embeddings]
# Opt-in semantic retrieval layer. Requires the [embeddings] pip extra.
enabled = false
ollama_base_url = "http://localhost:11434"
model = "nomic-embed-text"
```

**Precedence:** CLI flag > config file > default.

**Missing config:** Defaults used silently. No error.

---

## 9. Module layout

```
src/claude_recall/
├── __init__.py           # version export, public API surface
├── cli.py                # argparse entry point, dispatch to subcommands
├── storage.py            # SQLite connection, schema init, migrations
├── indexer.py            # JSONL walk + parse + insert
├── search.py             # FTS5 query, ranking, snippet generation
├── keywords.py           # (v0.2) keyword extraction for hook query building
├── embeddings.py         # (post-MVP) Ollama embedding client, semantic rerank
├── hooks/
│   ├── __init__.py
│   ├── session_start.sh        # bundled script, copied by init-hooks
│   ├── session_start.ps1       # Windows equivalent
│   ├── on_prompt.sh
│   └── on_prompt.ps1
└── config.py             # TOML load + defaults
```

**One module, one concern.** Keep `cli.py` thin — it only parses args and dispatches. Business logic lives in `indexer.py` / `search.py` / `storage.py`.

---

## 10. Implementation order

For the implementing agent. Each step is independently testable; do not proceed to the next until the current step passes tests.

1. **`storage.py`** — connection management, schema init, migration primitive. Acceptance: can create an empty DB, confirm schema v1, round-trip a session + messages through insert/select.
2. **`indexer.py`** — JSONL parse + incremental index. Acceptance: given a fixture JSONL with 20 messages, full index produces correct row counts; re-running with no changes produces 0 inserts; touching the file's mtime re-indexes just that file.
3. **`search.py`** — FTS5 query, BM25 ranking, snippet generation. Acceptance: given an indexed fixture, known queries return expected top results in expected order.
4. **`cli.py`** — wire subcommands. Acceptance: every command from §6 runs end-to-end against a fixture archive, `--format json` output validates against schemas.
5. **`config.py`** — TOML load, defaults, precedence. Acceptance: config values correctly override defaults; missing config uses defaults silently.
6. **Hook scripts** — bash and PowerShell. Acceptance: smoke tests that feed stdin JSON and validate stdout JSON; failure modes produce empty `{}` output, never exit non-zero.
7. **`init-hooks` command** — settings.json merge logic. Acceptance: creates hooks dir + files; merges into settings.json preserving existing hooks; backs up original.

---

## 11. Test plan

**Framework:** pytest. No other test deps required.

**Layout:**
```
tests/
├── __init__.py
├── conftest.py                 # fixtures: temp dir, sample DB, sample archive
├── fixtures/
│   ├── session_short.jsonl     # 5 messages, clean
│   ├── session_tool_blocks.jsonl  # includes tool_use / tool_result
│   ├── session_malformed.jsonl # one bad line to test graceful skip
│   └── session_large.jsonl     # 1,000 messages for perf smoke test
├── test_storage.py
├── test_indexer.py
├── test_search.py
├── test_cli.py                 # subprocess-invokes the CLI
├── test_config.py
└── test_hooks.py               # subprocess-invokes the shell hooks
```

**Key tests:**

- `test_indexer::test_malformed_line_does_not_crash` — parser skips bad lines, logs, continues.
- `test_indexer::test_incremental_skips_unchanged_files` — mtime-based skip works.
- `test_indexer::test_rebuild_replaces_cleanly` — old data gone, new data correct.
- `test_search::test_fts5_ranking_order` — known corpus produces known top-k.
- `test_search::test_threshold_filters_low_score` — BM25 threshold filtering.
- `test_search::test_snippet_marks_matches` — snippets include `<mark>` around hits.
- `test_cli::test_status_agent_context_format` — status line matches hook expected shape.
- `test_hooks::test_on_prompt_empty_on_error` — hook emits `{}` on any failure.
- `test_hooks::test_on_prompt_within_budget` — hook completes in < 500ms on sample archive.

**Coverage goal:** ≥ 80% on `storage.py`, `indexer.py`, `search.py`.

---

## 12. Acceptance criteria — MVP ship readiness

- [ ] All tests in §11 pass on Python 3.11, 3.12, 3.13.
- [ ] `pip install -e .` from a fresh venv succeeds. `claude-recall --version` prints `0.1.0`.
- [ ] `claude-recall index` completes in < 5 seconds for 100 sessions / ~10k messages.
- [ ] `claude-recall search` returns in < 100ms for typical queries on that corpus.
- [ ] SessionStart hook completes in < 2 seconds.
- [ ] UserPromptSubmit hook completes in < 500ms and **never blocks** the prompt flow (verified by fault-injection test).
- [ ] `claude-recall init-hooks` cleanly integrates into an existing `.claude/settings.json` without clobbering other hooks.
- [ ] README.md, ARCHITECTURE.md, INTEGRATION-GUIDE.md all reflect the shipped behavior.
- [ ] Works on Windows, macOS, Linux. Cross-platform tested.
- [ ] Zero external runtime dependencies for core install (`pip install claude-recall`).

---

## 13. Milestones

**v0.1.0 (MVP) — this scope.** Core indexer + CLI + two hooks + docs + tests. No embeddings. Release as GitHub tag, no PyPI yet.

**v0.2.0 — PyPI publish + refinements.** Keyword extraction in hooks (better FTS queries than dumping the whole prompt). Configurable hook templates. GitHub Action for CI. PyPI publish as `pip install claude-recall`.

**v0.3.0 — Embedding layer.** Opt-in semantic retrieval via Ollama `nomic-embed-text`. Hybrid retrieval (FTS5 candidates → embedding rerank). Adds `[embeddings]` pip extra.

**v0.4.0 — Cross-project search.** `--global` flag searches across all projects in the archive, not just current. Useful when prior insight from Project A applies to Project B.

**v1.0.0 — API stability + docs.** Lock the CLI surface and the hook contracts. Publish stable docs. Blog post + LinkedIn announcement land at or just before v1.

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Claude Code .jsonl schema drifts** | Indexer is defensive — unknown fields tolerated, malformed lines skipped with log. Schema validation on read, not on write. Document observed fields in ARCHITECTURE.md; track drift via a `--dry-run` flag that reports unparsable line count. |
| **Archive grows very large (>1M messages)** | FTS5 handles millions of rows. Add `VACUUM` and `PRAGMA optimize` post-index. Document `--rebuild` caveat. Consider per-project databases at v0.4+ if single-db approach stresses. |
| **Privacy concerns over indexing entire archive** | All data stays local. Document this prominently. v0.2 adds `.claude-recall-ignore` — path or regex-based exclusion. `index_tool_blocks = false` is default to minimize indexing of potentially-sensitive command outputs. |
| **Hook reliability — a crash would break Claude Code** | Every hook wraps the CLI invocation in failure-tolerant shell — `|| true`, `2>/dev/null`, catch-all exception handler. On any error, emit empty `{}`. This is tested by the fault-injection test in §11. |
| **FTS5 query syntax conflicts with natural-language prompts** | Hook uses the full prompt as query; FTS5 mostly tolerates this. When it fails, the search command auto-falls-back to a quoted-phrase search. Keyword extraction in v0.2 solves this properly. |
| **Cross-platform shell hook reliability** | Ship both `.sh` and `.ps1` variants. `init-hooks` detects OS and wires the appropriate one. Integration test matrix on GitHub Actions across Ubuntu, macOS, Windows. |
| **Schema version drift in future releases** | `schema_version` table + migration primitive in `storage.py`. Future versions add `ALTER TABLE` steps gated on current version. |

---

## 15. Security considerations

- **Archive is private data.** The index replicates whatever is in the `.jsonl` archive. Users who share a shell or sync `~/.config/` should understand this. Document clearly.
- **No network calls in MVP.** Confirm by grepping the codebase for `httpx`, `requests`, `urllib`, `socket` before each release — must be empty in MVP.
- **No elevated permissions required.** Runs as the user.
- **Database file permissions** — default `0600` (user-only read/write). `storage.py` should `os.chmod` after creation on Unix.
- **No secret detection.** MVP does not attempt to detect API keys or secrets in message content. A future `--redact` flag could run a redaction pass at query-time; out of scope for MVP.

---

## 16. Publication plan

1. **MVP complete** → tag `v0.1.0` on GitHub, no PyPI yet.
2. **Internal use for one week** — wire into ANI project (see INTEGRATION-GUIDE.md). Observe hook behavior, tune thresholds, catch edge cases.
3. **v0.2.0** — keyword extraction, PyPI publish as `pip install claude-recall`. Both Linux/macOS and Windows tested.
4. **Blog post** (draft at [BLOG-POST.md](BLOG-POST.md)) — "I kept re-explaining my project to Claude Code until I built claude-recall." Technical + personal framing. Publish on learnedgeek.com, cross-post to dev.to.
5. **LinkedIn post** (draft at [LINKEDIN-POST.md](LINKEDIN-POST.md)) — short form, drive traffic to the blog + GitHub.
6. **README promotion** — submit to awesome-claude-code lists, r/ClaudeAI, Claude Code Discord if one exists.
7. **v1.0.0** — after ~30 days of public use and feedback.

---

## 17. Open questions for review

Questions the implementing agent should raise to the project owner before making an irreversible choice:

1. **Keyword extraction (v0.2) approach.** POS-tagging via `spacy` or simpler regex-based noun chunking? `spacy` adds a meaningful dependency. Lightweight alternatives: `yake`, `rake-nltk`, or pure-stdlib regex + stopword filtering. Default recommendation: stdlib + stopword filter, unless initial testing shows too many low-quality hook queries.
2. **Whether to index tool_use / tool_result blocks by default.** Config flag defaults to `false`; revisit if users complain about missing context from Bash/Read calls they remember.
3. **GitHub org.** Publish under `LearnedGeek/claude-recall` or personal `mcarthey/claude-recall`? Aligns with LearnedGeek branding — recommend `LearnedGeek/claude-recall`. Confirm before `git remote add`.
4. **Analytics on usage.** Add opt-in telemetry (anonymized query count, hook firing rate) to understand real-world use? Recommend **no** for MVP, yes-with-explicit-opt-in for v1.0.

---

## 18. Starter commands for the implementing agent

```bash
# 1. Set up dev environment
cd E:/Documents/Work/dev/repos/claude-recall
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .[dev]

# 2. Run tests (should fail cleanly — no implementation yet)
pytest

# 3. Implement in the order from §10
#    Start with storage.py, then indexer.py, then search.py, then cli.py

# 4. Iterate: run tests after each module lands
pytest tests/test_storage.py -v

# 5. Once MVP passes all acceptance criteria in §12, tag release
git tag v0.1.0
git push --tags
```

---

**End of plan.** The companion documents — [ARCHITECTURE.md](ARCHITECTURE.md), [INTEGRATION-GUIDE.md](INTEGRATION-GUIDE.md), [BLOG-POST.md](BLOG-POST.md), [LINKEDIN-POST.md](LINKEDIN-POST.md) — add depth but are not required reading to start implementation. This document alone is sufficient.
