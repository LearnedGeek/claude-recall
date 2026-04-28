# Changelog

All notable changes to `claude-recall`. Format: one section per tag.

## v0.6.6 — 2026-04-28

Closes [#23](https://github.com/LearnedGeek/claude-recall/issues/23):
`claude-recall index` now auto-runs `embed` on the new tail when the
work is bounded and embeddings are enabled. Eliminates the manual
maintenance step that v0.5.5's stale-vector warning previously asked
users to take.

### Why

v0.6.0 made vectors survive routine re-ingest (content-hash diff
stopped the FK CASCADE on every index run). That fix closed the
catastrophic loss path. The remaining gap was that *new* messages
appended to active sessions still needed a manual `claude-recall
embed` to become semantically searchable. v0.5.5's loud warning when
coverage dropped below 95% surfaced the gap; this release closes it.

### Behavior

`claude-recall index` now runs an auto-embed pass at the end iff:

1. `[embeddings].enabled` is true
2. `--no-embed` flag was not passed
3. The new-message count is in `(0, 100]`
4. Ollama is reachable (probed via the existing helper)
5. The `[embeddings]` extra is importable

Above the 100-message threshold, the index step prints a hint to
stderr and exits clean — manual `claude-recall embed` is the right
shape for larger backfills (avoids surprising the user with multi-
minute waits inside `index`). The threshold is exposed as
`cli.AUTO_EMBED_THRESHOLD` so it's monkeypatchable for tests but not
configurable at runtime; chosen at 100 because that's roughly an
upper bound on per-session activity in steady-state Claude Code use.

If embed fails partway (Ollama hiccup, network blip), the index step
has already succeeded — embed failure is non-fatal, prints a hint to
stderr, exits 0. Manual retry via `claude-recall embed` recovers.

### `--no-embed` flag (and why SessionStart uses it)

A new `--no-embed` flag suppresses the auto-embed pass. Two consumers:

1. **The SessionStart hook.** `session_start.ps1` and `session_start.sh`
   both now call `claude-recall index --no-embed` instead of
   `claude-recall index`. Reasoning: SessionStart fires on every
   Claude Code session open, and the session-start latency budget
   can't afford a synchronous Ollama round trip every time. Manual
   `claude-recall index` (or the next embed-stale warning from
   `status`) handles embed instead.
2. **CI / scripted runs** that want strict separation between index
   and embed steps.

### Migration

```
pip install --upgrade claude-recall
claude-recall init-hooks --force
```

The `init-hooks --force` step is necessary on any project where you
want the SessionStart hook to use the new `--no-embed` flag — the
session_start.ps1/.sh in `.claude/hooks/` is a per-project copy that
init-hooks last propagated. v0.6.3's `--force` preserves user-added
sibling commands (time-injection hooks, etc.), so re-running is safe.

If you skip the `init-hooks --force`, your existing SessionStart hook
will still call `claude-recall index` without `--no-embed`. v0.6.6
will then auto-embed during session start, adding ~5-30s latency
depending on how many new messages accumulated since last open.
Functional, but slower — `--force` to refresh the per-project copy
fixes it.

### Tests

- `test_index_auto_embeds_small_tail_when_embeddings_enabled` — happy
  path: fresh index, embeddings enabled, fake Ollama; assert vectors
  populate to match message count.
- `test_index_no_embed_flag_skips_auto_embed` — flag suppresses the
  pass even when all other guards pass.
- `test_index_skips_auto_embed_above_threshold` — monkeypatched to
  threshold=5; 11-message fixture exceeds it; assert hint printed,
  no vectors created.
- `test_index_does_not_auto_embed_when_embeddings_disabled` — config
  with embeddings off; no auto-embed regardless of new-tail size.

180 Python tests pass; 62 C# tests pass; AOT binary builds clean.

## v0.6.5 — 2026-04-28

Hotfix for [#19](https://github.com/LearnedGeek/claude-recall/issues/19)
and [#22](https://github.com/LearnedGeek/claude-recall/issues/22):
the CLI now forces UTF-8 encoding on stdout and stderr at entry, so
non-ASCII characters in any output path (warning signs, arrows, em-
dashes, math symbols, user content) don't crash on Windows's default
cp1252 console without users having to set `PYTHONIOENCODING=utf-8`.

### The bug

#19 surfaced via `claude-recall list/search --format json` hitting the
`≥` symbol; #22 surfaced via plain `claude-recall search` hitting the
`→` arrow. Both manifested as:

```
UnicodeEncodeError: 'charmap' codec can't encode character '→' in position 343
```

…before any output reached the terminal. Same root cause: Python's
default stdout encoding on Windows is the console codepage (cp1252 in
US installs), which can't represent characters outside Latin-1 + a
small extension set. Any `print()` containing such a character raised
mid-output.

### Fix

Three lines at the top of `main()`:

```python
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
```

The C-API path through `TextIOWrapper.reconfigure()` sets
`sqlite3_busy_timeout`-equivalent encoding state directly, so the fix
applies regardless of `PYTHONIOENCODING` or console codepage. Guard
via `hasattr` because some test contexts replace stdout with a non-
text stream that doesn't support reconfigure; fall back silently in
that case rather than crashing the CLI on the safety net itself.

`errors="replace"` is defensive — UTF-8 can encode anything we throw
at it, but if some future code path hands stdout an already-encoded
byte sequence with stray bytes, we'd rather emit `?` than crash.

### Migration

```
pip install --upgrade claude-recall
```

No schema migration. Users who were setting `PYTHONIOENCODING=utf-8`
as a workaround can stop now.

### Tests

`test_main_forces_utf8_stdout_for_cp1252_safety` (in `tests/test_cli.py`)
constructs cp1252-encoded `TextIOWrapper` streams for stdout and stderr,
calls `main(["--version"])`, and asserts that:

1. Both streams' encoding flips to UTF-8 after `main()` runs.
2. Writing non-ASCII characters (`⚠ → ≥ — em-dash`) succeeds without
   `UnicodeEncodeError`.

Pre-fix the test would crash on the very first non-ASCII write to the
cp1252-strict TextIOWrapper. Post-fix it passes deterministically.

176 Python tests pass; 62 C# tests still pass; AOT binary still builds
clean (the binary path is independent of this fix — Console.WriteLine
on .NET defaults to UTF-8 already, only the Python CLI was affected).

## v0.6.4 — 2026-04-27

Hotfix for [issue #21](https://github.com/LearnedGeek/claude-recall/issues/21):
claude-recall's hook output was using the legacy top-level
`additionalContext` shape, which Claude Code's strict-validation pass
silently drops. **The hook was firing and the JSON was correct, but
the additionalContext never reached the model.** Same shape as #15
applied to the output side instead of the input side.

### The bug

All three claude-recall hook output paths (`search.py:_format_agent_context`,
`session_start.ps1`, `session_start.sh`, and the NativeAOT binary's
`Program.cs:WriteAgentContext`) emitted:

```json
{"additionalContext": "..."}
```

Claude Code's hook output schema requires the wrapped form:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "..."
  }
}
```

Top-level was accepted leniently by older Claude Code versions; the
strict pass introduced alongside [Claude Code v2.1.118](https://github.com/anthropics/claude-code/releases/tag/v2.1.118)
silently drops it. **No error, no log, no warning.** The hook ran
successfully but the injection went nowhere. This was diagnosable only
by:

1. Verifying the hook output JSON by hand (which would say "correct"),
2. Then noticing that Claude couldn't actually see the injected context
   (e.g., asking "what time is it?" and getting a stale answer),
3. Then realizing the format was the gap.

The maintainer's session received hard evidence during triage: switching
a custom user hook to the wrapped form caused Claude Code to surface
the injection as a labeled `system-reminder` (`UserPromptSubmit hook
additional context: ...`). The same labeling was never appearing for
claude-recall's own output, confirming the legacy shape was being
silently dropped.

### Fixed

- **`search.py:_format_agent_context`** — wraps under
  `hookSpecificOutput` with `hookEventName: "UserPromptSubmit"`.
- **`session_start.ps1` and `session_start.sh`** — wrap under
  `hookSpecificOutput` with `hookEventName: "SessionStart"`.
- **C# `Program.cs:WriteAgentContext`** — new `HookSpecificOutput`
  class, updated `AgentContextEnvelope` to nest it, source generator
  registration extended.

All four paths now emit the canonical schema-correct shape. The
NativeAOT binary in the Windows wheel is rebuilt by CI on tag push,
so users `pip install --upgrade claude-recall` will get a binary that
emits the wrapped form automatically — no `init-hooks --force`
required for the binary itself, though re-running it doesn't hurt and
will refresh the binary copy in `.claude/hooks/`.

### Migration

```
pip install --upgrade claude-recall
```

If you have user-added sibling hooks (e.g., a time-injection hook),
make sure they also use the wrapped form — see issue #21 for the
PowerShell snippet. v0.6.3's `init-hooks --force` preserves your
sibling hooks, so this works cleanly.

### Tests

- `test_format_agent_context_with_results` updated to assert wrapped
  envelope shape and explicitly reject the top-level legacy form.
- `test_session_start_hook_emits_valid_json` updated similarly with
  the `hookEventName: "SessionStart"` assertion.
- Negative assertion (`additionalContext NOT at top level`) makes the
  test loud if anyone reverts to the legacy shape.

175 Python tests pass; 62 C# tests pass; AOT binary builds clean.

## v0.6.3 — 2026-04-26

Hotfix for [issue #20](https://github.com/LearnedGeek/claude-recall/issues/20):
`init-hooks --force` was silently destroying user-added sibling commands
composed within the same managed event as the claude-recall hook.

### The bug

`--force` did `hooks_block.pop(event)` for the two events claude-recall
manages (`SessionStart`, `UserPromptSubmit`), then re-merged just our
own command back. Wiping the entire matcher entry destroyed any user
commands sitting alongside ours in the same `hooks: [...]` array.

This compounded with v0.5.5+ actively prompting users to run
`init-hooks --force` whenever a stale-hook warning fired — making the
destruction both silent and routine. DC's reproduction: composed a
time-injection PowerShell hook alongside the claude-recall hook;
upgraded 0.6.0 → 0.6.2 on the staleness prompt; ran `init-hooks
--force`; time hook was gone.

### The fix

`--force` now surgically removes only claude-recall-owned commands
within each managed event, leaving sibling commands intact. We
identify our commands by filename fragment match (`claude-recall-hook`,
`session_start.ps1`, `session_start.sh`, `on_prompt.ps1`,
`on_prompt.sh`) so the heuristic survives install-path shifts across
versions.

If a matcher entry's `hooks: [...]` array becomes empty after stripping
ours, the entry is dropped. If the event has no entries left after
that, the event key is dropped. Net effect: users layering custom
hooks alongside ours are now safe to run `init-hooks --force` whenever
the stale-hook warning fires.

### Migration

```
pip install --upgrade claude-recall
```

If you previously had user-added hooks within `UserPromptSubmit` or
`SessionStart` that were lost to a prior `--force`, you'll need to
re-add them by hand — v0.6.3 prevents the destruction going forward,
but doesn't restore from history. The common pattern people are
asking about (current-time injection) is a one-line PowerShell hook;
see issue #20 for the snippet.

### Tests

- `test_init_hooks_force_preserves_user_added_sibling_commands`
  ([tests/test_cli.py](https://github.com/LearnedGeek/claude-recall/blob/main/tests/test_cli.py)) —
  pre-state has both a user-managed time-injection hook and a stale
  claude-recall path within the same matcher entry. After `--force`,
  the user hook survives, the stale path is gone, and the new
  claude-recall path is present.

175 tests pass (was 174; one new).

## v0.6.2 — 2026-04-26

CI fix-forward for v0.6.1. The v0.6.1 build failed on the
concurrent-indexer regression test on Windows CI; debugging the
failure surfaced a real concurrency-hardening gap and a test-shape
issue. Both fixed here.

**v0.6.1 never published to PyPI** — CI failed before the publish
step, so PyPI users go straight from 0.6.0 to 0.6.2. The git tag
`v0.6.1` exists for historical record but represents an unreleased
intermediate. Read this entry alongside the v0.6.1 entry below for
the full set of fixes shipping in 0.6.2.

### Fixed (v0.6.2-specific)

- **Connection-level busy timeout set at connect time, not via PRAGMA.**
  `sqlite3.connect()` now passes `timeout=30.0` to push the busy
  timeout through the C-API's `sqlite3_busy_timeout` directly. The
  PRAGMA path (which v0.6.1 used) doesn't reliably apply to BEGIN
  IMMEDIATE in Python's sqlite3 module on Windows. The v0.6 BEGIN
  IMMEDIATE wrapping was necessary but not sufficient without this.
  PRAGMA `busy_timeout = 30000` is also set as belt-and-suspenders
  for any code path that re-reads the connection's timeout.
- **`_index_file` retries BEGIN IMMEDIATE on transient lock errors.**
  Wallclock-bounded retry loop with 50ms backoff bridges any gap
  between the connection's busy_timeout and Python sqlite3's wrapper
  behavior. Total wait bounded at 30 seconds — production indexer
  contention realistically completes in milliseconds, so this only
  fires in pathological cases.
- **Concurrent-indexer regression test rewrote to pre-create the DB.**
  Two threads simultaneously calling `PRAGMA journal_mode = WAL` on a
  fresh DB raced on the WAL-mode conversion (each thread tries to
  acquire EXCLUSIVE briefly to flip the journal mode, and the
  conversion isn't well-protected by busy_timeout). Pre-creating the
  DB before launching the threads matches production reality (by the
  time concurrent indexers run, the DB exists from a prior bootstrap)
  and removes the spurious failure mode without weakening the
  no-duplicates safety assertion.

### Migration

```
pip install --upgrade claude-recall
```

Same as v0.6.1. No schema migration. No re-embed.

## v0.6.1 — 2026-04-26

Three small fixes closing the diagnostic threads from the v0.6.0
weekend. Each is well-scoped and bounded; bundling rather than shipping
three separate patches.

### Migration

```
pip install --upgrade claude-recall
```

No schema migration. No re-embed required. The `--days` filter behavior
changes semantically (per-message rather than per-session — see [#13](https://github.com/LearnedGeek/claude-recall/issues/13)
below), which is a fix not a regression but worth knowing about if you
were previously seeing different result counts.

### Fixed

- **[#13](https://github.com/LearnedGeek/claude-recall/issues/13) — `--project` filter returning zero matches against long-lived sessions.**
  The date predicate in [`search.py`](https://github.com/LearnedGeek/claude-recall/blob/main/src/claude_recall/search.py)
  was filtering on `sessions.started_at` (the session's *first* message
  timestamp) rather than `messages.timestamp` (the individual message's
  timestamp). For sessions spanning many months — like OC's CrewTrack
  archive (5,622-turn single session) — the entire session fell outside
  the default 90-day window, even when many of its individual messages
  were recent. The fix flips the predicate to `m.timestamp >= ?`, which
  is the semantically-correct interpretation of `--days N`. The existing
  `idx_messages_timestamp` index keeps it cheap.

  Diagnostic credit: OC's bare-SQL queries (Q1 — per-project hit
  distribution; Q2 — slug hex dump) ruled out the originally-hypothesized
  causes (slug encoding, session_id mismatch) and pointed at "the CLI
  must be running different SQL than the bare query." That observation
  led directly to the missing predicate.

- **[#17](https://github.com/LearnedGeek/claude-recall/issues/17) — orphan session rows from removed JSONLs.**
  `run_index()` now sweeps session rows whose `file_path` no longer
  points at an existing JSONL. The FK CASCADE on `sessions.session_id`
  handles `messages` and `message_vectors` cleanup automatically. Scoped
  to the projects walked in this index run — `--project foo` won't
  delete orphans from project bar. New `IndexReport.deleted_sessions`
  field tracks how many were swept.

  Empirical signal that motivated this: DC's v0.6.0 verification turned
  up a 4-of-21 (~19%) orphan rate on a real-world archive — high enough
  to be worth fixing rather than living with as a known limitation.

- **[#18](https://github.com/LearnedGeek/claude-recall/issues/18) — `status --integrity-check` per-project SUM(turn_count) cartesian-inflated.**
  The diagnostic-tool itself had a bug: the per-project query joined
  `messages` directly under the outer `GROUP BY`, producing a
  cartesian explosion (for a session with N messages, the join
  produced N rows each carrying turn_count=N, summed = N²). Every
  project on a healthy archive falsely flagged as mismatched. Fixed
  by aggregating per-session first via a CTE, then summing per-project.
  False-positive `⚠ turn_count/messages mismatch` warnings will stop
  appearing on healthy archives.

  Diagnostic credit: the inflation pattern was unmistakable in OC's
  output — every single-session project's `stored_turns` exactly equaled
  `actual_msgs²` (1672² = 2,795,584 for WCTC; 5768² = 33,269,824 for
  CrewTrack; etc.). The math locked the diagnosis in one read.

### Tests

- `test_days_filter_uses_message_timestamp_not_session_started_at`
  ([tests/test_search.py](https://github.com/LearnedGeek/claude-recall/blob/main/tests/test_search.py)) —
  constructs a synthetic long-lived session whose `started_at` is 180
  days old but whose recent messages are 5 days old. Asserts that
  scoped search with `--days 30` surfaces the recent messages and not
  the old ones.
- `test_orphan_session_sweep_removes_deleted_files` and
  `test_orphan_sweep_scoped_to_walked_projects`
  ([tests/test_indexer.py](https://github.com/LearnedGeek/claude-recall/blob/main/tests/test_indexer.py)) —
  cover the cascade behavior (messages and vectors cleared along with
  the orphan session row) and the project-scope guard.
- `test_status_integrity_check_per_project_no_cartesian_inflation`
  ([tests/test_cli.py](https://github.com/LearnedGeek/claude-recall/blob/main/tests/test_cli.py)) —
  parses the per-project line for the multi-session fixture and
  asserts `stored_turns == actual_msgs`, plus that the false-positive
  warning is absent.

Total: 174 tests pass (was 170 — 4 new). 1 platform-skipped.

### Deferred

- **[#19](https://github.com/LearnedGeek/claude-recall/issues/19) — cp1252 UnicodeEncodeError on Windows for non-ASCII content.**
  Filed during this triage pass but not bundled into v0.6.1 — different
  layer of the stack (CLI output encoding), worth its own focused fix
  in a v0.6.2 or later.

## v0.6.0 — 2026-04-25

Architectural fix for [issue #16](https://github.com/LearnedGeek/claude-recall/issues/16):
the indexer no longer cascade-wipes vectors on routine re-ingest. v0.5.5
made the orphan situation visible (honest `embeddings_ready`, prominent
"vectors are stale" warnings); v0.6 makes it stop happening. The schema
migrates non-destructively — existing vectors are preserved across the
upgrade.

### Migration

```
pip install --upgrade claude-recall
```

That's it. The schema migration runs automatically on the next `open_db()`
call (which happens implicitly the first time you run any claude-recall
command after upgrading). It adds a `content_hash` column to `messages`
and backfills it from existing content, so all your existing
`(msg_id, vector)` pairs stay valid. **No re-embed required.** No data
loss. The first re-index after upgrade still incurs a one-time write
(populating hashes for the new column), but vectors are not touched.

> **Note:** if your `SessionStart` hook is wired (the default after
> `init-hooks`), the hook calls `claude-recall status` on every Claude
> Code session start, which opens the DB and triggers the migration
> immediately. So users who want to capture true pre-migration state
> via `claude-recall status --format json` should do so *before* opening
> Claude Code in any project. By the time you can hand-run a `status`
> command after upgrading, the hook has likely already run the migration
> behind the scenes. Not a problem for verification (the migration is
> non-destructive), but worth knowing.

### Fixed

- **Indexer hot path no longer cascade-deletes vectors on routine
  re-ingest.** The DELETE-then-INSERT pattern at the heart of `_index_file`
  has been replaced with a content-hash diff: only messages whose hash
  actually differs from the stored hash get DELETEd, so the FK CASCADE
  on `message_vectors.msg_id` only fires for messages that genuinely
  changed. Compaction events (where Claude Code rewrites the early lines
  of a session JSONL with a summary turn) correctly delete the vectors
  for the compacted-away turns and leave the unchanged tail intact.
  Append-only sessions (the common case) leave all existing vectors in
  place and only insert new rows for the appended turns.
- **`mtime` change without content change is correctly identified as a
  no-op.** Previously, any mtime touch (atomic-write rewrite, `touch(1)`,
  backup-restore clock skew) forced a full session re-ingest with cascade
  vector loss. Now the hash diff sees no changes and reports the session
  as `unchanged`, with only `sessions.file_mtime` updated so the fast path
  engages on subsequent runs.
- **Concurrent indexer runs no longer race.** A SessionStart hook firing
  in parallel with a manual `claude-recall index` previously could
  double-insert messages. v0.6 wraps the per-file diff+update in a
  `BEGIN IMMEDIATE` transaction so the read-then-write is atomic.
- **WAL mode is now enabled** on the index DB, so concurrent readers
  (e.g., a `status` query during an indexer run) don't block on the
  writer's lock.

### Added

- **`messages.content_hash` column** (blake2b, 16-byte digest) — the
  per-message change detector that powers the hash-diff path. Backfilled
  during the v1→v2 migration from existing content.
- **`IndexReport.incremental_sessions`** field — sessions that took the
  hash-diff path and produced a non-empty delta. Distinguishes from
  `updated_sessions` (full re-ingest, fires only on first index or
  `--rebuild`) and `unchanged_sessions` (no content delta). CLI summary
  output now reports this when verbose.
- **`init-hooks` stale-vector warning** — mirrors the v0.5.5 status warning
  so a user who upgrades and re-runs `init-hooks` discovers the embed step
  without consulting `status` separately. Auto-running embed itself is
  not enabled — multi-minute work users wouldn't expect from
  `init-hooks` — but the diagnostic surface is free.
- **Schema migration infrastructure.** `_install_schema` now runs DDL,
  detects current version, runs any pending migrations, then version-stamps
  last (so a partial migration leaves the version behind and the next
  open retries). The v1→v2 migration is the first user.

### Tests

- 10 new regression tests in `tests/test_indexer.py`, including the
  compaction-event repro that drove the choice of content-hash diff over
  append-only line counting:
  - `test_append_only_normal_case_preserves_existing_vectors`
  - `test_compaction_event_replaces_prefix_preserves_tail`
  - `test_edit_mid_stream_re_embeds_only_affected_turn`
  - `test_mtime_jitter_without_content_change_is_noop`
  - `test_malformed_line_does_not_break_session_in_v06`
  - `test_session_file_deleted_orphan_vectors_cleaned_up`
  - `test_v1_to_v2_migration_preserves_vectors`
  - `test_concurrent_index_runs_do_not_double_insert`
  - `test_init_hooks_warns_when_coverage_below_threshold`
  - `test_full_lifecycle_index_embed_append_index_preserves_coverage`
- Existing `test_changed_file_triggers_reindex` updated to reflect the
  v0.6 contract (mtime touch without content change → `unchanged_sessions`).

### Known limitations

- **Compaction mechanism still empirically unconfirmed.** DC's correction
  that JSONL is mid-stream-editable (rather than strictly append-only)
  is the load-bearing assumption that drove the choice of content-hash
  diff. The diff handles all three plausible compaction shapes
  correctly (rewrite-in-place, append-marker, new-session-id), so the
  uncertainty is academic — but worth flagging for future readers.
- **Session deletion sweep is not in v0.6.** If a session JSONL is removed
  from disk, the corresponding session row (and its vectors) stays in
  the DB until next `--rebuild`. Low-priority follow-up; not load-bearing
  for the search results since deleted sessions just stop matching the
  archive walk.

## v0.5.5 — 2026-04-25

Surface-honesty hotfix for [issue #16](https://github.com/LearnedGeek/claude-recall/issues/16).
ANI's diagnostic surfaced a longstanding architectural quirk: routine
re-indexing of session JSONLs cascades through the foreign key on
`message_vectors.msg_id`, silently wiping vectors anytime a session
file's mtime changes (which happens whenever a new turn is appended).
The vectors come back the next time `claude-recall embed` runs, but
between cascade and re-embed the tool was reporting `embeddings_ready:
True` while semantic search returned "no vectors in index" — two reads
of the same DB giving contradictory answers.

This release ships the **honest-status surface fix only**. The
underlying cascade-then-re-embed dance is architectural and will be
addressed in v0.6 with a content-hash-aware indexer that doesn't wipe
vectors on routine re-ingest. For now: status no longer lies, and the
user gets a clear pointer at the right command to run.

### Migration

If you upgraded from an older v0.5.x and your `status` reports a
coverage gap, run:

```
claude-recall embed
```

This re-embeds messages that have no vector (i.e., the ones the
indexer's CASCADE wiped during routine re-ingest). On a 25,000-message
archive this takes ~4 minutes against a warm Ollama. Re-running
`claude-recall init-hooks --force` (the v0.5.4 migration step) is
**not** required for this issue — `init-hooks` doesn't touch the
index DB.

### Fixed

- **`embeddings_ready` now requires ≥95% vector coverage**, not just
  `vectors_indexed > 0`. Reporting "ready" at 16% coverage was
  silently deceptive — search returned "no vectors" because the
  surviving vectors didn't intersect the FTS5 candidate pool, while
  status said everything was fine. The 5% slack absorbs in-flight
  index-vs-embed races without masking the real orphan-after-cascade
  case.
- **`status` text format prints a `vectors_coverage` percentage**
  alongside the existing counts, and prints a prominent
  `vectors are stale: ...` block when coverage drops below the
  threshold — mirroring the existing `hooks are stale` warning.
- **`status --format agent-context` now reports coverage percentage**
  in its degraded-state line, so the SessionStart hook bubbles the
  diagnostic up to the active Claude instance instead of silently
  injecting an "embeddings ready" lie.

### Added

- `vectors_coverage` field in `status --format json` output (ratio
  in `[0, 1]`, rounded to 4 decimal places). Programmatic consumers
  can branch on this directly.

### Known limitations (deferred to v0.6)

- The cascade-on-re-ingest mechanism itself is not addressed in this
  release. Until v0.6 ships, users should expect to run
  `claude-recall embed` periodically (or after any extended
  Claude Code activity that doesn't end with an explicit re-embed)
  to keep coverage high. The v0.5.5 status output now makes that
  visible instead of hiding it behind a lying `embeddings_ready`.
- A scheduled `embed` is the simplest workaround if you don't want
  to monitor manually. e.g., `claude-recall embed` after each
  major Claude session, or as a cron job on systems where claude-recall
  is wired into multiple projects.

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
