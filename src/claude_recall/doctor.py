"""Diagnose hook wiring issues (v0.9.2, issue #30).

When a project's `.claude/settings.json` drifts from the canonical shape
claude-recall's `init-hooks` emits, the hook can silently stop firing —
no error to the user, no log line, no fallback. The user-visible symptom
is *"claude-recall context isn't being injected"* which looks like any
of a dozen unrelated things.

`claude-recall doctor` reads the project's settings, validates each
hook entry against the schema claude-recall knows, and reports specific
drift with file:event:command precision. Catches the entire failure
class in ~5 seconds instead of 30-45 minutes of filesystem archaeology.

What this catches:

- Missing nested `hooks: [...]` array (the flat shape Claude Code's
  strict-validation pass silently rejects — issue #21's failure mode).
- Missing `type: "command"` field on inner entries.
- Missing `shell:` field on `.ps1` / `.sh` commands (bash can't execute
  PowerShell scripts without it).
- Command paths that don't exist on disk.
- Claude-recall-owned commands at stale paths (drift from where
  `init-hooks --force` would re-emit them today).

What this doesn't catch (yet):

- Whether Claude Code is actually invoking the hook in practice (would
  need filesystem-trace instrumentation; deferred to v0.9.3 `--trace`).
- Whether the hook's stdout shape matches what Claude Code accepts.
- Anything about the hook's *behavior* once invoked.

Exit codes:
  0  — all checks pass (settings clean)
  1  — warnings only (e.g., user-added commands the linter doesn't
       recognize but can't say are wrong)
  2  — errors (schema violations that will silently break the hook)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Settings file name lives in cli.py; we use the canonical value here
# to avoid an import cycle.
_SETTINGS_FILENAME = "settings.json"


@dataclass
class Finding:
    severity: str            # "OK" | "WARN" | "ERROR"
    event: str               # e.g. "UserPromptSubmit" or "(top-level)"
    message: str
    detail: str = ""


@dataclass
class DoctorReport:
    settings_path: Path
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "ERROR" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == "WARN" for f in self.findings)


def run_doctor(project_root: Path) -> DoctorReport:
    """Walk the project's settings.json and report any schema/path drift."""
    settings_path = project_root / ".claude" / _SETTINGS_FILENAME
    report = DoctorReport(settings_path=settings_path)

    if not settings_path.exists():
        report.findings.append(Finding(
            severity="ERROR",
            event="(top-level)",
            message=f"settings file does not exist: {settings_path}",
            detail="Run `claude-recall init-hooks` to scaffold one.",
        ))
        return report

    try:
        raw = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.findings.append(Finding(
            severity="ERROR",
            event="(top-level)",
            message=f"cannot read settings file: {exc}",
        ))
        return report

    try:
        settings = json.loads(raw)
    except json.JSONDecodeError as exc:
        report.findings.append(Finding(
            severity="ERROR",
            event="(top-level)",
            message=f"settings.json is not valid JSON: {exc}",
            detail=(
                "Restore from the .bak file if init-hooks created one. "
                "Manually-edited settings.json with trailing commas or "
                "unquoted keys will fail Claude Code's parser too."
            ),
        ))
        return report

    if not isinstance(settings, dict):
        report.findings.append(Finding(
            severity="ERROR",
            event="(top-level)",
            message="settings.json root must be a JSON object",
        ))
        return report

    hooks_block = settings.get("hooks")
    if hooks_block is None:
        report.findings.append(Finding(
            severity="WARN",
            event="(top-level)",
            message="no `hooks` block in settings.json",
            detail=(
                "claude-recall hooks are not registered. "
                "Run `claude-recall init-hooks` to wire them up."
            ),
        ))
        return report

    if not isinstance(hooks_block, dict):
        report.findings.append(Finding(
            severity="ERROR",
            event="hooks",
            message="`hooks` must be a JSON object",
        ))
        return report

    for event_name, entries in hooks_block.items():
        _check_event(event_name, entries, report)

    if not report.findings:
        report.findings.append(Finding(
            severity="OK",
            event="(top-level)",
            message="all hook entries validate cleanly",
        ))
    return report


def _check_event(event_name: str, entries, report: DoctorReport) -> None:
    if not isinstance(entries, list):
        report.findings.append(Finding(
            severity="ERROR",
            event=event_name,
            message=f"event value must be a JSON array, got {type(entries).__name__}",
        ))
        return

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            report.findings.append(Finding(
                severity="ERROR",
                event=event_name,
                message=f"entry #{idx} must be a JSON object",
            ))
            continue
        inner = entry.get("hooks")
        if inner is None:
            # Flat shape — the silent-rejection failure mode from issue #21/#30.
            top_level_cmd = entry.get("command")
            if top_level_cmd:
                report.findings.append(Finding(
                    severity="ERROR",
                    event=event_name,
                    message=(
                        f"entry #{idx} uses flat shape (top-level `command`) "
                        f"instead of the nested `hooks: [...]` array Claude "
                        f"Code's strict parser requires"
                    ),
                    detail=(
                        f"command: {top_level_cmd!r}. Wrap in "
                        f'`{{ "hooks": [{{ "type": "command", "command": ... }}] }}`. '
                        f"Run `init-hooks --force` to regenerate, or fix by hand."
                    ),
                ))
            else:
                report.findings.append(Finding(
                    severity="ERROR",
                    event=event_name,
                    message=f"entry #{idx} missing required `hooks` array",
                ))
            continue

        if not isinstance(inner, list):
            report.findings.append(Finding(
                severity="ERROR",
                event=event_name,
                message=(
                    f"entry #{idx} `hooks` must be a JSON array, "
                    f"got {type(inner).__name__}"
                ),
            ))
            continue

        for sub_idx, h in enumerate(inner):
            _check_inner_hook(event_name, idx, sub_idx, h, report)


def _check_inner_hook(
    event_name: str, entry_idx: int, sub_idx: int,
    h, report: DoctorReport,
) -> None:
    label = f"entry #{entry_idx}.hooks[{sub_idx}]"

    if not isinstance(h, dict):
        report.findings.append(Finding(
            severity="ERROR",
            event=event_name,
            message=f"{label} must be a JSON object",
        ))
        return

    if h.get("type") != "command":
        report.findings.append(Finding(
            severity="ERROR",
            event=event_name,
            message=f"{label} missing `type: \"command\"` field",
            detail=f"got type={h.get('type')!r}",
        ))

    cmd = h.get("command")
    if not isinstance(cmd, str) or not cmd:
        report.findings.append(Finding(
            severity="ERROR",
            event=event_name,
            message=f"{label} missing or empty `command` field",
        ))
        return

    # PowerShell / shell-script commands need an explicit shell hint.
    lower = cmd.lower()
    needs_shell = (
        lower.endswith(".ps1") or ".ps1 " in lower
        or lower.endswith(".sh") or ".sh " in lower
    )
    has_shell = "shell" in h
    if needs_shell and not has_shell:
        report.findings.append(Finding(
            severity="ERROR",
            event=event_name,
            message=(
                f"{label} is a script command (.ps1 or .sh) without "
                f"`shell` field — Claude Code's default shell can't "
                f"execute it directly"
            ),
            detail=(
                f"command: {cmd!r}. Add `\"shell\": \"powershell\"` for .ps1 "
                f"or `\"shell\": \"bash\"` for .sh."
            ),
        ))

    # Check if a path-based command points at something that exists.
    # Inline expressions (PowerShell @{...}-style) and bare entry-point
    # names (`claude-recall ...`) aren't paths and don't need this check.
    if _looks_like_path(cmd):
        head = _extract_path_head(cmd)
        if head and not Path(head).exists():
            report.findings.append(Finding(
                severity="ERROR",
                event=event_name,
                message=(
                    f"{label} command path does not exist on disk"
                ),
                detail=(
                    f"path: {head!r}. The hook will fail silently when "
                    f"Claude Code tries to invoke it. Likely a stale path "
                    f"left over after upgrade or repo move — "
                    f"`claude-recall init-hooks --force` will refresh it."
                ),
            ))


def _looks_like_path(cmd: str) -> bool:
    # A "path" is one that contains a directory separator or ends with
    # a known script/binary extension. Naked `claude-recall ...` calls
    # are entry-point lookups, not paths.
    if cmd.startswith(("@{", "${", "{")):
        return False  # inline expression
    head = cmd.split(None, 1)[0]
    if "\\" in head or "/" in head:
        return True
    lower = head.lower()
    return lower.endswith((".exe", ".ps1", ".sh", ".bat", ".cmd"))


def _extract_path_head(cmd: str) -> str:
    """Return the first whitespace-delimited token, stripping surrounding quotes."""
    head = cmd.split(None, 1)[0]
    if head.startswith('"') and head.endswith('"') and len(head) >= 2:
        head = head[1:-1]
    return head


def format_report(report: DoctorReport) -> str:
    lines = [f"settings: {report.settings_path}", ""]
    if not report.findings:
        lines.append("no findings")
        return "\n".join(lines)
    counts = {"OK": 0, "WARN": 0, "ERROR": 0}
    for f in report.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
        lines.append(f"[{f.severity:5}] {f.event}: {f.message}")
        if f.detail:
            lines.append(f"          {f.detail}")
    lines.append("")
    summary_bits = []
    if counts["ERROR"]:
        summary_bits.append(f"{counts['ERROR']} error(s)")
    if counts["WARN"]:
        summary_bits.append(f"{counts['WARN']} warning(s)")
    if counts["OK"] and not summary_bits:
        summary_bits.append("OK")
    lines.append("summary: " + ", ".join(summary_bits))
    return "\n".join(lines)
