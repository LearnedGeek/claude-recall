#!/usr/bin/env bash
# claude-recall SessionStart hook (Unix)
#
# Runs at the start of every Claude Code session.
# Does an incremental index update, then emits a one-line status as additionalContext.
#
# Failure policy: any error collapses to silent no-op. Never block session start.

set +e
exec 2>/dev/null

# Silent incremental index
claude-recall index >/dev/null 2>&1 || true

# Status line
STATUS=$(claude-recall status --format agent-context 2>/dev/null || echo "claude-recall: unavailable")

# Issue #21 (v0.6.4): wrap in hookSpecificOutput. Top-level
# `additionalContext` was the legacy form Claude Code accepted leniently;
# the strict-validation pass alongside v2.1.118's hook-schema tightening
# silently drops top-level. Canonical wrapped shape works everywhere.
cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "claude-recall status: $STATUS\n\nPrior-session archive is searchable via the \`claude-recall search <query>\` CLI, and matching context will be auto-injected when your prompt references prior work."
  }
}
EOF

exit 0
