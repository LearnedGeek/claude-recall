# claude-recall UserPromptSubmit hook (Windows PowerShell)
#
# Runs on every user prompt. Auto-queries the index and injects relevant prior-session
# context as additionalContext when matches cross the configured threshold.
#
# Failure policy: on ANY error, emit empty JSON and exit 0. Never block user prompts.

$ErrorActionPreference = 'SilentlyContinue'

try {
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw) { Write-Output '{}'; exit 0 }

    $parsed = $raw | ConvertFrom-Json
    $prompt = $parsed.prompt
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
