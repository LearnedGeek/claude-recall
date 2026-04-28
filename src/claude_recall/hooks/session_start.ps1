# claude-recall SessionStart hook (Windows PowerShell)
#
# Runs at the start of every Claude Code session.
# Does an incremental index update, then emits a one-line status as additionalContext.
#
# Failure policy: any error collapses to silent no-op. Never block session start.

$ErrorActionPreference = 'SilentlyContinue'

try {
    # Issue #23 (v0.6.6): pass --no-embed because index now auto-embeds
    # small tails when embeddings are enabled. SessionStart fires on
    # every Claude Code session open; we can't afford a synchronous
    # Ollama round-trip in the session-start latency budget. The user's
    # next manual `claude-recall index` (or the next embed-stale warning
    # from `status`) handles embed instead.
    & claude-recall index --no-embed 2>$null | Out-Null
} catch {
    # Swallow. Index failures should not block session start.
}

$status = & claude-recall status --format agent-context 2>$null
if (-not $status) { $status = "claude-recall: unavailable" }

# Issue #21 (v0.6.4): wrap in hookSpecificOutput. Top-level
# `additionalContext` was the legacy form Claude Code accepted leniently;
# the strict-validation pass alongside v2.1.118's hook-schema tightening
# silently drops top-level. Canonical wrapped shape works everywhere.
$out = @{
    hookSpecificOutput = @{
        hookEventName = 'SessionStart'
        additionalContext = "claude-recall status: $status`n`nPrior-session archive is searchable via the ``claude-recall search <query>`` CLI, and matching context will be auto-injected when your prompt references prior work."
    }
}
$out | ConvertTo-Json -Compress

exit 0
