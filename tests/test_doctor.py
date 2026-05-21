"""Tests for the doctor subcommand (v0.9.2, issue #30)."""

from __future__ import annotations

import json
from pathlib import Path

from claude_recall import doctor


def _write_settings(project_root: Path, settings: dict) -> Path:
    """Create .claude/settings.json with the given content."""
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings_path


def test_doctor_reports_missing_settings_file(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    report = doctor.run_doctor(project)
    assert report.has_errors
    msgs = [f.message for f in report.findings]
    assert any("does not exist" in m for m in msgs)


def test_doctor_reports_malformed_json(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        "{this is not, valid json", encoding="utf-8",
    )
    report = doctor.run_doctor(project)
    assert report.has_errors
    msgs = [f.message for f in report.findings]
    assert any("not valid JSON" in m for m in msgs)


def test_doctor_warns_when_no_hooks_block(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, {"other": "thing"})
    report = doctor.run_doctor(project)
    assert report.has_warnings
    assert not report.has_errors
    msgs = [f.message for f in report.findings]
    assert any("no `hooks` block" in m for m in msgs)


def test_doctor_passes_clean_canonical_shape(tmp_path):
    """The shape claude-recall init-hooks emits should validate cleanly."""
    project = tmp_path / "proj"
    # Create a real script so the path-exists check passes.
    (project / ".claude" / "hooks").mkdir(parents=True)
    script = project / ".claude" / "hooks" / "session_start.ps1"
    script.write_text("# stub", encoding="utf-8")

    _write_settings(project, {
        "hooks": {
            "SessionStart": [{
                "matcher": "startup|resume|compact",
                "hooks": [{
                    "type": "command",
                    "command": str(script),
                    "shell": "powershell",
                }],
            }],
        },
    })
    report = doctor.run_doctor(project)
    assert not report.has_errors
    assert not report.has_warnings


def test_doctor_catches_flat_shape_drift(tmp_path):
    """Issue #30 root cause: flat `command` instead of nested `hooks` array.
    Claude Code's strict parser silently rejects this; doctor must call it
    out as ERROR."""
    project = tmp_path / "proj"
    _write_settings(project, {
        "hooks": {
            "UserPromptSubmit": [{
                "command": "E:\\path\\to\\on_prompt.ps1",
            }],
        },
    })
    report = doctor.run_doctor(project)
    assert report.has_errors
    msgs = [f.message for f in report.findings]
    assert any("flat shape" in m for m in msgs)


def test_doctor_catches_missing_shell_field_on_ps1(tmp_path):
    """A .ps1 command without `shell` field silently fails on default bash."""
    project = tmp_path / "proj"
    (project / ".claude" / "hooks").mkdir(parents=True)
    script = project / ".claude" / "hooks" / "on_prompt.ps1"
    script.write_text("# stub", encoding="utf-8")

    _write_settings(project, {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{
                    "type": "command",
                    "command": str(script),
                    # missing "shell": "powershell"
                }],
            }],
        },
    })
    report = doctor.run_doctor(project)
    assert report.has_errors
    msgs = [f.message for f in report.findings]
    assert any("script command" in m and "shell" in m for m in msgs)


def test_doctor_catches_stale_command_path(tmp_path):
    """A command pointing at a path that doesn't exist on disk fires silent
    when Claude Code tries to invoke it. doctor surfaces this as ERROR."""
    project = tmp_path / "proj"
    _write_settings(project, {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{
                    "type": "command",
                    "command": "E:\\nonexistent\\path\\hook.exe",
                }],
            }],
        },
    })
    report = doctor.run_doctor(project)
    assert report.has_errors
    msgs = [f.message for f in report.findings]
    assert any("does not exist on disk" in m for m in msgs)


def test_doctor_catches_missing_type_field(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude" / "hooks").mkdir(parents=True)
    script = project / ".claude" / "hooks" / "session_start.ps1"
    script.write_text("# stub", encoding="utf-8")

    _write_settings(project, {
        "hooks": {
            "SessionStart": [{
                "hooks": [{
                    # missing "type": "command"
                    "command": str(script),
                    "shell": "powershell",
                }],
            }],
        },
    })
    report = doctor.run_doctor(project)
    assert report.has_errors
    msgs = [f.message for f in report.findings]
    assert any("`type: \"command\"`" in m for m in msgs)


def test_doctor_naked_claude_recall_invocation_does_not_path_check(tmp_path):
    """`claude-recall emit-prompt-context --__cr-managed` is an entry-point
    invocation, not a path. doctor must not flag it as missing on disk."""
    project = tmp_path / "proj"
    _write_settings(project, {
        "hooks": {
            "UserPromptSubmit": [{
                "hooks": [{
                    "type": "command",
                    "command": "claude-recall emit-prompt-context --__cr-managed",
                }],
            }],
        },
    })
    report = doctor.run_doctor(project)
    # Should NOT flag command-path-does-not-exist (it's not a path).
    path_findings = [f for f in report.findings if "does not exist on disk" in f.message]
    assert not path_findings, (
        f"naked entry-point command flagged as missing path: "
        f"{[f.message for f in path_findings]}"
    )


def test_doctor_format_report_includes_severity_counts(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, {
        "hooks": {
            "UserPromptSubmit": [{
                "command": "/missing.ps1",  # flat shape — ERROR
            }],
        },
    })
    report = doctor.run_doctor(project)
    text = doctor.format_report(report)
    assert "ERROR" in text
    assert "summary:" in text
    assert "error(s)" in text


def test_doctor_cli_returns_2_on_errors(tmp_path):
    """Exit code 2 on any error finding so users can script around it."""
    from claude_recall import cli
    project = tmp_path / "proj"
    _write_settings(project, {
        "hooks": {
            "UserPromptSubmit": [{
                "command": "/missing/path.ps1",
            }],
        },
    })
    code = cli.main(["doctor", "--project-root", str(project)])
    assert code == 2


def test_doctor_cli_returns_0_on_clean(tmp_path):
    from claude_recall import cli
    project = tmp_path / "proj"
    (project / ".claude" / "hooks").mkdir(parents=True)
    script = project / ".claude" / "hooks" / "session_start.ps1"
    script.write_text("# stub", encoding="utf-8")
    _write_settings(project, {
        "hooks": {
            "SessionStart": [{
                "matcher": "startup|resume|compact",
                "hooks": [{
                    "type": "command",
                    "command": str(script),
                    "shell": "powershell",
                }],
            }],
        },
    })
    code = cli.main(["doctor", "--project-root", str(project)])
    assert code == 0


def test_doctor_cli_returns_1_on_warnings_only(tmp_path):
    """No hooks block → WARN, no ERROR → exit 1."""
    from claude_recall import cli
    project = tmp_path / "proj"
    _write_settings(project, {"other": "thing"})
    code = cli.main(["doctor", "--project-root", str(project)])
    assert code == 1
