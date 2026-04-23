"""Tests for claude_recall.cli.

See docs/PLAN.md sections 6 (CLI spec), 10.4, and 11 (test plan).
"""

import json
import shutil
from pathlib import Path

import pytest

from claude_recall import cli

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def cli_env(tmp_path, monkeypatch, capsys):
    """Populate a full CLI environment: config file, archive dir, db path."""
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

    config_toml = tmp_path / "config.toml"
    config_toml.write_text(
        f"""
[archive]
root = "{archive_root.as_posix()}"

[database]
path = "{db_path.as_posix()}"
""",
        encoding="utf-8",
    )

    def run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(["--config", str(config_toml), *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return {
        "archive_root": archive_root,
        "db_path": db_path,
        "config_toml": config_toml,
        "run": run,
    }


def test_version_flag_prints_semver(capsys):
    """--version prints the package version and exits 0."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    from claude_recall import __version__
    assert __version__ in out


def test_index_then_search_end_to_end(cli_env):
    """claude-recall index; then claude-recall search returns expected hits."""
    code, out, _ = cli_env["run"]("index")
    assert code == 0
    assert "indexed" in out.lower()

    code, out, _ = cli_env["run"]("search", "regex", "--format", "json")
    assert code == 0
    payload = json.loads(out)
    assert payload["query"] == "regex"
    assert payload["total_matches"] >= 1


def test_index_missing_archive_exits_1(tmp_path, capsys):
    """Pointing index at a non-existent archive root exits 1."""
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        f'[archive]\nroot = "{(tmp_path / "missing").as_posix()}"\n'
        f'[database]\npath = "{(tmp_path / "db.sqlite").as_posix()}"\n',
        encoding="utf-8",
    )
    code = cli.main(["--config", str(cfg_path), "index"])
    err = capsys.readouterr().err
    assert code == 1
    assert "archive root does not exist" in err


def test_search_invalid_query_exits_2(cli_env):
    """A query with no usable tokens exits 2 (invalid FTS5)."""
    cli_env["run"]("index")
    code, _, err = cli_env["run"]("search", "()!!!")
    assert code == 2
    assert "invalid query" in err


def test_search_extract_keywords_flag_end_to_end(cli_env):
    """--extract-keywords strips fillers and matches for a natural-language prompt."""
    cli_env["run"]("index")
    code, out, _ = cli_env["run"](
        "search",
        "remind me what we decided about regex patterns",
        "--extract-keywords",
        "--format",
        "json",
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["total_matches"] >= 1


def test_search_agent_context_empty_returns_braces(cli_env):
    """--agent-context with no matches prints literal '{}'."""
    cli_env["run"]("index")
    code, out, _ = cli_env["run"](
        "search", "absolutely-no-such-word-xyzzy", "--agent-context"
    )
    assert code == 0
    assert out.strip() == "{}"


def test_show_existing_session_json(cli_env):
    cli_env["run"]("index")
    code, out, _ = cli_env["run"]("show", "session_short", "--format", "json")
    assert code == 0
    payload = json.loads(out)
    assert payload["session_id"] == "session_short"
    assert len(payload["messages"]) == 5


def test_show_missing_session_exits_1(cli_env):
    cli_env["run"]("index")
    code, _, err = cli_env["run"]("show", "nonesuch")
    assert code == 1
    assert "session not found" in err


def test_show_turn_range_slices(cli_env):
    cli_env["run"]("index")
    code, out, _ = cli_env["run"](
        "show", "session_short", "--format", "json", "--turns", "0-1"
    )
    payload = json.loads(out)
    assert [m["turn_index"] for m in payload["messages"]] == [0, 1]


def test_list_json(cli_env):
    cli_env["run"]("index")
    code, out, _ = cli_env["run"]("list", "--format", "json")
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 3
    assert all("session_id" in r for r in rows)


def test_list_text_empty_db_prints_no_sessions(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[archive]\nroot = "{tmp_path.as_posix()}"\n'
        f'[database]\npath = "{(tmp_path / "db.sqlite").as_posix()}"\n',
        encoding="utf-8",
    )
    code = cli.main(["--config", str(cfg), "list"])
    assert code == 0
    assert "no sessions indexed." in capsys.readouterr().out


def test_status_json(cli_env):
    cli_env["run"]("index")
    code, out, _ = cli_env["run"]("status", "--format", "json")
    assert code == 0
    payload = json.loads(out)
    assert payload["total_sessions"] == 3
    assert payload["schema_version"] == 1
    assert payload["checks"]["archive_accessible"] is True
    assert payload["checks"]["db_accessible"] is True
    assert payload["checks"]["fts_available"] is True
    assert payload["checks"]["schema_current"] is True


def test_status_agent_context_format(cli_env):
    """status --format agent-context matches the shape the SessionStart hook expects."""
    cli_env["run"]("index")
    code, out, _ = cli_env["run"]("status", "--format", "agent-context")
    assert code == 0
    line = out.strip()
    assert line.startswith("claude-recall:")
    assert "sessions" in line
    assert "messages indexed" in line


def test_status_text_format(cli_env):
    cli_env["run"]("index")
    code, out, _ = cli_env["run"]("status")
    assert code == 0
    assert "archive_root:" in out
    assert "checks:" in out


def test_search_project_auto_scopes_to_indexed_slug(cli_env, monkeypatch):
    """--project auto resolves to the stored slug for the indexed fixture project."""
    cli_env["run"]("index")
    # The fixture indexer stores the slug as "test-project" (literal dir name).
    # Chdir to a path whose derived slug will NOT match test-project, confirming
    # the DB lookup falls through to canonical and yields no matches.
    import tempfile
    tmp = tempfile.mkdtemp()
    monkeypatch.chdir(tmp)
    code, out, _ = cli_env["run"](
        "search", "regex", "--project", "auto", "--format", "json"
    )
    assert code == 0
    payload = json.loads(out)
    # cwd is not our indexed project — 0 matches proves the scope was applied.
    assert payload["total_matches"] == 0


def test_search_project_explicit_slug_still_works(cli_env):
    """The pre-existing --project <slug> form continues to filter exactly."""
    cli_env["run"]("index")
    code, out, _ = cli_env["run"](
        "search", "regex", "--project", "test-project", "--format", "json"
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["total_matches"] >= 1


def test_search_from_config_uses_hook_values(tmp_path, monkeypatch, capsys):
    """--from-config resolves unspecified flags from [search].hook_* in config.toml."""
    import shutil as _shutil
    archive_root = tmp_path / "archive"
    project = archive_root / "test-project"
    project.mkdir(parents=True)
    for name in (
        "session_short.jsonl",
        "session_malformed.jsonl",
        "session_tool_blocks.jsonl",
    ):
        _shutil.copy(FIXTURES_DIR / name, project / name)

    db_path = tmp_path / "index.db"
    cfg_path = tmp_path / "config.toml"
    # Set hook_limit=2 via config — expect limit=2 when --from-config is passed
    cfg_path.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n'
        '[search]\nhook_limit = 2\nhook_days = 365\nhook_threshold = 0.0\n',
        encoding="utf-8",
    )

    cli.main(["--config", str(cfg_path), "index"])
    capsys.readouterr()

    cli.main([
        "--config", str(cfg_path),
        "search", "regex OR patterns OR decided",
        "--from-config",
        "--format", "json",
    ])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["returned"] <= 2  # capped by hook_limit


def test_search_cli_flag_overrides_from_config(tmp_path, capsys):
    """Explicit CLI flags take precedence over config values when --from-config is set."""
    import shutil as _shutil
    archive_root = tmp_path / "archive"
    project = archive_root / "test-project"
    project.mkdir(parents=True)
    _shutil.copy(FIXTURES_DIR / "session_short.jsonl", project / "session_short.jsonl")

    db_path = tmp_path / "index.db"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n'
        '[search]\nhook_limit = 2\nhook_days = 365\nhook_threshold = 0.0\n',
        encoding="utf-8",
    )

    cli.main(["--config", str(cfg_path), "index"])
    capsys.readouterr()

    # Explicit --limit 1 should override hook_limit=2
    cli.main([
        "--config", str(cfg_path),
        "search", "regex",
        "--from-config",
        "--limit", "1",
        "--format", "json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert payload["returned"] <= 1


def test_init_hooks_writes_version_stamp(tmp_path):
    """init-hooks writes the installed package version alongside the scripts."""
    from claude_recall import __version__ as pkg_version
    project_root = tmp_path / "proj"
    project_root.mkdir()
    cli.main(["init-hooks", "--project-root", str(project_root)])
    stamp = project_root / ".claude" / "hooks" / ".claude-recall-version"
    assert stamp.exists()
    assert stamp.read_text(encoding="utf-8").strip() == pkg_version


def test_status_agent_context_warns_when_hooks_stale(tmp_path, monkeypatch, capsys):
    """status --format agent-context mentions stale hooks when the stamp disagrees."""
    import shutil as _shutil
    archive_root = tmp_path / "archive"
    project = archive_root / "test-project"
    project.mkdir(parents=True)
    _shutil.copy(FIXTURES_DIR / "session_short.jsonl", project / "session_short.jsonl")

    db_path = tmp_path / "index.db"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n',
        encoding="utf-8",
    )

    # Install hooks, then fake an old stamp
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    cli.main(["init-hooks", "--project-root", str(project_root)])
    capsys.readouterr()
    (project_root / ".claude" / "hooks" / ".claude-recall-version").write_text(
        "0.0.1-ancient\n", encoding="utf-8"
    )

    cli.main(["--config", str(cfg_path), "index"])
    capsys.readouterr()

    monkeypatch.chdir(project_root)
    cli.main(["--config", str(cfg_path), "status", "--format", "agent-context"])
    out = capsys.readouterr().out
    assert "0.0.1-ancient" in out
    assert "init-hooks --force" in out


def test_init_hooks_creates_scripts_and_merges_settings(tmp_path, capsys):
    """init-hooks copies the hook scripts and merges hooks into settings.json."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 0

    hooks_dir = project_root / ".claude" / "hooks"
    assert hooks_dir.is_dir()
    # At least one script per platform
    scripts = list(hooks_dir.iterdir())
    assert scripts

    settings = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert "SessionStart" in settings["hooks"]
    assert "UserPromptSubmit" in settings["hooks"]


def test_init_hooks_preserves_existing_hooks(tmp_path, capsys):
    """init-hooks merges into an existing settings.json without clobbering other hooks."""
    project_root = tmp_path / "proj"
    (project_root / ".claude").mkdir(parents=True)
    existing = {
        "hooks": {
            "PreToolUse": [{"command": "/some/other/script.sh"}],
            "SessionStart": [{"command": "/pre-existing.sh", "matcher": "startup"}],
        }
    }
    settings_path = project_root / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(existing), encoding="utf-8")

    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 0
    merged = json.loads(settings_path.read_text(encoding="utf-8"))
    # Pre-existing PreToolUse still there
    assert merged["hooks"]["PreToolUse"] == [{"command": "/some/other/script.sh"}]
    # SessionStart has both the pre-existing and our new one
    commands = {e["command"] for e in merged["hooks"]["SessionStart"]}
    assert "/pre-existing.sh" in commands
    assert any("session_start" in c for c in commands)

    # Backup file created
    assert (project_root / ".claude" / "settings.json.bak").exists()


def test_init_hooks_idempotent(tmp_path):
    """Running init-hooks twice does not create duplicate SessionStart entries."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    cli.main(["init-hooks", "--project-root", str(project_root)])
    cli.main(["init-hooks", "--project-root", str(project_root), "--force"])
    settings = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert len(settings["hooks"]["SessionStart"]) == 1
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1


def test_init_hooks_aborts_on_bad_json(tmp_path, capsys):
    """init-hooks refuses to touch invalid settings.json and returns non-zero."""
    project_root = tmp_path / "proj"
    (project_root / ".claude").mkdir(parents=True)
    (project_root / ".claude" / "settings.json").write_text(
        "not valid json at all", encoding="utf-8"
    )
    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 1
    assert "not valid JSON" in capsys.readouterr().err
