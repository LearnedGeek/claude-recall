"""Tests for the shell hook scripts.

These tests subprocess-invoke the hook scripts with controlled stdin and
verify that stdout is valid JSON matching the hook contract from docs/PLAN.md
section 7.

See docs/PLAN.md section 11 for the full test plan.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "src" / "claude_recall" / "hooks"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
BUDGET_SECONDS = 0.5
SESSION_START_BUDGET_SECONDS = 2.0

# venv Scripts or bin dir where claude-recall.exe lives
_VENV_BIN = Path(sys.executable).parent


def _have_bash() -> bool:
    return shutil.which("bash") is not None


def _have_pwsh() -> bool:
    return shutil.which("pwsh") is not None or shutil.which("powershell") is not None


def _is_windows() -> bool:
    return sys.platform == "win32"


def _pwsh_exe() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _hook_cmd(hook_name: str) -> list[str] | None:
    """Return the platform-appropriate command to run a hook script."""
    if _is_windows() and _have_pwsh():
        return [
            _pwsh_exe(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(HOOKS_DIR / f"{hook_name}.ps1"),
        ]
    if not _is_windows() and _have_bash():
        return ["bash", str(HOOKS_DIR / f"{hook_name}.sh")]
    return None


def _make_config(tmp_path: Path, archive_root: Path, db_path: Path) -> Path:
    """Write a config.toml into tmp_path/claude-recall/config.toml."""
    cfg_dir = tmp_path / "claude-recall"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.toml"
    cfg_path.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n',
        encoding="utf-8",
    )
    return cfg_path


def _hook_env(config_home: Path, path_has_cli: bool = True) -> dict:
    """Build an env dict that points claude-recall at a test config.

    If path_has_cli is False, removes the venv Scripts dir from PATH so the
    hooks can't find claude-recall — used for fault-injection tests.
    """
    env = os.environ.copy()
    if _is_windows():
        env["APPDATA"] = str(config_home)
    else:
        env["XDG_CONFIG_HOME"] = str(config_home)
    current_path = env.get("PATH", "")
    if path_has_cli:
        env["PATH"] = str(_VENV_BIN) + os.pathsep + current_path
    else:
        # Strip any directory whose name matches Scripts/bin to defeat the CLI.
        parts = [
            p for p in current_path.split(os.pathsep) if Path(p) != _VENV_BIN
        ]
        env["PATH"] = os.pathsep.join(parts)
    return env


@pytest.fixture
def hook_env(tmp_path):
    """Build a fully configured test environment for hook invocation."""
    archive_root = tmp_path / "archive"
    project = archive_root / "test-project"
    project.mkdir(parents=True)
    for name in (
        "session_short.jsonl",
        "session_malformed.jsonl",
        "session_tool_blocks.jsonl",
    ):
        shutil.copy(FIXTURES_DIR / name, project / name)

    db_path = tmp_path / "index.db"
    config_home = tmp_path / "cfg"
    _make_config(config_home, archive_root, db_path)

    # Pre-build the index so search returns results predictably.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "claude_recall.cli",
            "--config",
            str(config_home / "claude-recall" / "config.toml"),
            "index",
        ],
        check=True,
        capture_output=True,
    )

    return {
        "archive_root": archive_root,
        "db_path": db_path,
        "config_home": config_home,
        "env": _hook_env(config_home),
    }


def _run_hook(cmd: list[str], env: dict, stdin_bytes: bytes = b"") -> tuple[int, str]:
    """Invoke a hook script with timing budget enforced by subprocess timeout."""
    proc = subprocess.run(
        cmd,
        input=stdin_bytes,
        env=env,
        capture_output=True,
        timeout=5,  # hard cap so a broken hook can't hang the suite
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


@pytest.mark.skipif(
    _hook_cmd("session_start") is None,
    reason="no bash or pwsh available on this host",
)
def test_session_start_hook_emits_valid_json(hook_env):
    """SessionStart hook stdout parses as JSON with additionalContext field."""
    cmd = _hook_cmd("session_start")
    code, out = _run_hook(cmd, env=hook_env["env"])
    assert code == 0
    payload = json.loads(out)
    assert "additionalContext" in payload
    assert "claude-recall" in payload["additionalContext"]


@pytest.mark.skipif(
    _hook_cmd("session_start") is None,
    reason="no bash or pwsh available on this host",
)
def test_session_start_hook_within_budget(hook_env):
    """SessionStart hook completes in < 2s on an already-indexed sample archive."""
    cmd = _hook_cmd("session_start")
    start = time.monotonic()
    code, _ = _run_hook(cmd, env=hook_env["env"])
    elapsed = time.monotonic() - start
    assert code == 0
    assert elapsed < SESSION_START_BUDGET_SECONDS, (
        f"SessionStart hook took {elapsed:.2f}s (budget {SESSION_START_BUDGET_SECONDS}s)"
    )


@pytest.mark.skipif(
    _hook_cmd("on_prompt") is None,
    reason="no bash or pwsh available on this host",
)
def test_on_prompt_hook_emits_valid_json(hook_env):
    """UserPromptSubmit hook stdout parses as JSON on a realistic prompt."""
    cmd = _hook_cmd("on_prompt")
    prompt = json.dumps({"prompt": "What did we decide about regex patterns?"})
    code, out = _run_hook(cmd, env=hook_env["env"], stdin_bytes=prompt.encode("utf-8"))
    assert code == 0
    # Either {} (no match above threshold) or {additionalContext: ...}
    payload = json.loads(out)
    assert isinstance(payload, dict)


@pytest.mark.skipif(
    _hook_cmd("on_prompt") is None,
    reason="no bash or pwsh available on this host",
)
def test_on_prompt_empty_stdin_yields_braces(hook_env):
    """Empty stdin yields literal '{}' and exit 0."""
    cmd = _hook_cmd("on_prompt")
    code, out = _run_hook(cmd, env=hook_env["env"], stdin_bytes=b"")
    assert code == 0
    assert out.strip() == "{}"


@pytest.mark.skipif(
    _hook_cmd("on_prompt") is None,
    reason="no bash or pwsh available on this host",
)
def test_on_prompt_empty_on_error(hook_env):
    """When claude-recall is missing from PATH, the hook still exits 0 with '{}'."""
    cmd = _hook_cmd("on_prompt")
    broken_env = _hook_env(hook_env["config_home"], path_has_cli=False)
    prompt = json.dumps({"prompt": "something to search"})
    code, out = _run_hook(cmd, env=broken_env, stdin_bytes=prompt.encode("utf-8"))
    assert code == 0, f"hook exited nonzero with cli missing: stdout={out!r}"
    assert out.strip() == "{}"


@pytest.mark.skipif(
    _hook_cmd("on_prompt") is None,
    reason="no bash or pwsh available on this host",
)
def test_on_prompt_within_budget(hook_env):
    """UserPromptSubmit hook completes in < 500ms on a sample indexed archive."""
    cmd = _hook_cmd("on_prompt")
    prompt = json.dumps({"prompt": "regex"}).encode("utf-8")
    # Warm any shell/script caching with one throwaway invocation.
    _run_hook(cmd, env=hook_env["env"], stdin_bytes=prompt)
    start = time.monotonic()
    code, _ = _run_hook(cmd, env=hook_env["env"], stdin_bytes=prompt)
    elapsed = time.monotonic() - start
    assert code == 0
    assert elapsed < BUDGET_SECONDS, (
        f"UserPromptSubmit hook took {elapsed:.2f}s (budget {BUDGET_SECONDS}s)"
    )
