#!/usr/bin/env bash
# claude-recall UserPromptSubmit hook (Unix)
#
# Runs on every user prompt. Auto-queries the index and injects relevant prior-session
# context as additionalContext when matches cross the configured threshold.
#
# Failure policy: on ANY error, emit empty JSON and exit 0. Never block user prompts.

set +e
exec 2>/dev/null

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

# Call search with the full prompt as query.
# v0.2 will add keyword extraction; MVP uses raw prompt.
RESULT=$(claude-recall search "$PROMPT" --project auto --from-config --extract-keywords --agent-context 2>/dev/null)

if [ -z "$RESULT" ] || [ "$RESULT" = '{}' ]; then
    echo '{}'
    exit 0
fi

echo "$RESULT"
exit 0
