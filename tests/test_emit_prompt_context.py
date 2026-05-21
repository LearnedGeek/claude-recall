"""Tests for the emit-prompt-context hook composer (v0.9.0, issue #29).

The subcommand reads a prompt envelope from stdin and emits a wrapped
hookSpecificOutput JSON containing:
  - interventions content (if inject_interventions=true and file present)
  - claude-recall search results (when matches cross threshold)
  - merged with `\\n\\n---\\n\\n` separator when both are present

Failure policy: any error → print '{}' exit 0. Never block the prompt.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from claude_recall import cli, indexer, storage


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _build_config(tmp_path: Path, archive_root: Path, db_path: Path,
                  *, inject: bool = False,
                  interventions_path: str | None = None) -> Path:
    cfg_path = tmp_path / "config.toml"
    # Permissive search settings: tests need deterministic recall against
    # fixtures whose dates are outside the default 30-day window. Real-world
    # day-filtering is exercised by the regular search tests.
    body = (
        f"[archive]\nroot = \"{archive_root.as_posix()}\"\n"
        f"[database]\npath = \"{db_path.as_posix()}\"\n"
        f"[search]\nhook_days = 36500\nhook_threshold = 0.0\nhook_limit = 5\n"
    )
    if inject:
        body += "[hooks]\ninject_interventions = true\n"
        if interventions_path:
            body += f"interventions_path = \"{interventions_path}\"\n"
    cfg_path.write_text(body, encoding="utf-8")
    return cfg_path


def _run_emit(cfg_path: Path, stdin_text: str, capsys, monkeypatch) -> tuple[int, str]:
    """Invoke `claude-recall emit-prompt-context` with the given stdin."""
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = cli.main(["--config", str(cfg_path), "emit-prompt-context"])
    captured = capsys.readouterr()
    return code, captured.out


def _seed_archive_and_index(tmp_path: Path) -> tuple[Path, Path]:
    """Copy fixture sessions and run indexer. Returns (archive_root, db_path)."""
    archive_root = tmp_path / "archive"
    project = archive_root / "test-project"
    project.mkdir(parents=True)
    import shutil
    for name in ("session_short.jsonl",):
        shutil.copy(FIXTURES_DIR / name, project / name)
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        indexer.run_index(conn, archive_root)
    finally:
        conn.close()
    return archive_root, db_path


def test_emit_empty_stdin_emits_empty_json(tmp_path, capsys, monkeypatch):
    """Empty stdin → '{}'. No DB access, no error."""
    cfg_path = _build_config(tmp_path, tmp_path / "archive", tmp_path / "db.sqlite")
    code, out = _run_emit(cfg_path, "", capsys, monkeypatch)
    assert code == 0
    assert out.strip() == "{}"


def test_emit_malformed_json_emits_empty_json(tmp_path, capsys, monkeypatch):
    """Malformed JSON on stdin → '{}'. Never crashes."""
    cfg_path = _build_config(tmp_path, tmp_path / "archive", tmp_path / "db.sqlite")
    code, out = _run_emit(cfg_path, "this is not json", capsys, monkeypatch)
    assert code == 0
    assert out.strip() == "{}"


def test_emit_missing_prompt_field_emits_empty_json(tmp_path, capsys, monkeypatch):
    """JSON envelope without `prompt` field → '{}'."""
    cfg_path = _build_config(tmp_path, tmp_path / "archive", tmp_path / "db.sqlite")
    code, out = _run_emit(cfg_path, json.dumps({"other": "thing"}), capsys, monkeypatch)
    assert code == 0
    assert out.strip() == "{}"


def test_emit_omits_interventions_when_flag_false(tmp_path, capsys, monkeypatch):
    """inject_interventions=false (default): file content is ignored even
    if present. Backward-compatible with pre-v0.9 hook setups."""
    archive_root, db_path = _seed_archive_and_index(tmp_path)
    cfg_path = _build_config(tmp_path, archive_root, db_path, inject=False)
    # Create an interventions file at the default location.
    project_dir = tmp_path / "proj"
    (project_dir / ".claude" / "hooks").mkdir(parents=True)
    (project_dir / ".claude" / "hooks" / "interventions.md").write_text(
        "DO NOT IGNORE: this should NOT appear", encoding="utf-8",
    )
    monkeypatch.chdir(project_dir)
    code, out = _run_emit(
        cfg_path,
        json.dumps({"prompt": "regex"}),
        capsys, monkeypatch,
    )
    assert code == 0
    # interventions content must not leak into the output
    assert "DO NOT IGNORE" not in out


def test_emit_injects_interventions_when_flag_true_and_file_present(
    tmp_path, capsys, monkeypatch,
):
    """inject_interventions=true + file with content → content appears in
    additionalContext, prepended before any recall content."""
    archive_root, db_path = _seed_archive_and_index(tmp_path)
    cfg_path = _build_config(tmp_path, archive_root, db_path, inject=True)
    project_dir = tmp_path / "proj"
    (project_dir / ".claude" / "hooks").mkdir(parents=True)
    (project_dir / ".claude" / "hooks" / "interventions.md").write_text(
        "INTERVENTION-SENTINEL-7349", encoding="utf-8",
    )
    monkeypatch.chdir(project_dir)
    code, out = _run_emit(
        cfg_path,
        json.dumps({"prompt": "anything"}),
        capsys, monkeypatch,
    )
    assert code == 0
    payload = json.loads(out)
    assert "hookSpecificOutput" in payload
    assert "INTERVENTION-SENTINEL-7349" in payload["hookSpecificOutput"]["additionalContext"]


def test_emit_falls_back_to_recall_only_when_interventions_file_missing(
    tmp_path, capsys, monkeypatch,
):
    """inject_interventions=true but file doesn't exist → no error, recall-
    only behavior. Hook policy: never block the prompt."""
    archive_root, db_path = _seed_archive_and_index(tmp_path)
    cfg_path = _build_config(tmp_path, archive_root, db_path, inject=True)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # No interventions.md created — file is missing
    monkeypatch.chdir(project_dir)
    code, out = _run_emit(
        cfg_path,
        json.dumps({"prompt": "regex"}),
        capsys, monkeypatch,
    )
    assert code == 0
    # Either '{}' (recall also empty) or wrapped JSON with recall only.
    # Either way, no crash, no error.


def test_emit_blank_interventions_file_treated_as_missing(
    tmp_path, capsys, monkeypatch,
):
    """Whitespace-only interventions file → same as missing."""
    archive_root, db_path = _seed_archive_and_index(tmp_path)
    cfg_path = _build_config(tmp_path, archive_root, db_path, inject=True)
    project_dir = tmp_path / "proj"
    (project_dir / ".claude" / "hooks").mkdir(parents=True)
    (project_dir / ".claude" / "hooks" / "interventions.md").write_text(
        "   \n\n   \n", encoding="utf-8",
    )
    monkeypatch.chdir(project_dir)
    code, out = _run_emit(
        cfg_path,
        json.dumps({"prompt": "regex"}),
        capsys, monkeypatch,
    )
    assert code == 0
    # No interventions content should appear; only recall (which may be empty)
    if out.strip() != "{}":
        payload = json.loads(out)
        # If recall returned anything, its body must not start with whitespace-
        # filler — the merge should treat blank interventions as absent.
        assert "Relevant prior-session" in payload["hookSpecificOutput"]["additionalContext"]


def test_emit_merges_interventions_and_recall_with_separator(
    tmp_path, capsys, monkeypatch,
):
    """Both interventions and recall present → joined with '\\n\\n---\\n\\n'
    separator, interventions FIRST (load-bearing position before drafting).

    Seeds the DB with a session whose project_slug matches the cwd-derived
    slug, so the project-auto-scoped search inside emit-prompt-context
    actually finds it."""
    from claude_recall import projects as _projects

    db_path = tmp_path / "index.db"
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # Compute the slug the way emit-prompt-context will compute it from cwd.
    expected_slug = _projects.slug_from_path(project_dir.absolute())

    conn = storage.open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions(session_id, project_slug, file_path, "
            "file_mtime, started_at, ended_at, turn_count, indexed_at) "
            "VALUES (?, ?, ?, 0, ?, ?, 0, ?)",
            ("merge-test-sess", expected_slug,
             "/test/merge.jsonl", "2026-05-21", "2026-05-21", "2026-05-21"),
        )
        conn.execute(
            "INSERT INTO messages(session_id, role, content, turn_index, "
            "timestamp, content_hash, content_kind) "
            "VALUES (?,?,?,?,?,?,?)",
            ("merge-test-sess", "user",
             "discussion of regex patterns and architecture", 0,
             "2026-05-21", "h0", "THOUGHT"),
        )
        conn.commit()
    finally:
        conn.close()

    cfg_path = _build_config(tmp_path, archive_root, db_path, inject=True)
    (project_dir / ".claude" / "hooks").mkdir(parents=True)
    (project_dir / ".claude" / "hooks" / "interventions.md").write_text(
        "FIRST-INTERVENTION-MARKER", encoding="utf-8",
    )
    monkeypatch.chdir(project_dir)
    code, out = _run_emit(
        cfg_path,
        json.dumps({"prompt": "regex patterns"}),
        capsys, monkeypatch,
    )
    assert code == 0
    payload = json.loads(out)
    body = payload["hookSpecificOutput"]["additionalContext"]
    assert "FIRST-INTERVENTION-MARKER" in body
    assert "\n\n---\n\n" in body
    # Interventions come first (structurally before drafting)
    sep_idx = body.index("\n\n---\n\n")
    assert body.index("FIRST-INTERVENTION-MARKER") < sep_idx
    # Recall content comes after
    assert "Relevant prior-session" in body[sep_idx:]


def test_emit_interventions_path_override_resolves_against_cwd(
    tmp_path, capsys, monkeypatch,
):
    """Non-default interventions_path resolves correctly relative to cwd."""
    archive_root, db_path = _seed_archive_and_index(tmp_path)
    cfg_path = _build_config(
        tmp_path, archive_root, db_path,
        inject=True, interventions_path="custom/notes.md",
    )
    project_dir = tmp_path / "proj"
    (project_dir / "custom").mkdir(parents=True)
    (project_dir / "custom" / "notes.md").write_text(
        "CUSTOM-PATH-SENTINEL", encoding="utf-8",
    )
    monkeypatch.chdir(project_dir)
    code, out = _run_emit(
        cfg_path,
        json.dumps({"prompt": "anything"}),
        capsys, monkeypatch,
    )
    assert code == 0
    payload = json.loads(out)
    assert "CUSTOM-PATH-SENTINEL" in payload["hookSpecificOutput"]["additionalContext"]


def test_emit_accepts_managed_marker_flag_as_noop(tmp_path, capsys, monkeypatch):
    """The hidden `--__cr-managed` flag is silently accepted by argparse and
    does not affect behavior. Required for init-hooks-emitted commands."""
    archive_root, db_path = _seed_archive_and_index(tmp_path)
    cfg_path = _build_config(tmp_path, archive_root, db_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "regex"})))
    code = cli.main([
        "--config", str(cfg_path), "emit-prompt-context", "--__cr-managed",
    ])
    captured = capsys.readouterr()
    assert code == 0
    # No crash; output is either '{}' or wrapped JSON.


def test_init_hooks_uses_python_composer_when_inject_interventions_true(
    tmp_path, monkeypatch,
):
    """Issue #29: when inject_interventions=true, init-hooks emits the Python
    `emit-prompt-context` subcommand for UserPromptSubmit (not the binary
    fast-path). Verifies the routing decision."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[hooks]\ninject_interventions = true\n",
        encoding="utf-8",
    )
    code = cli.main([
        "--config", str(cfg_path),
        "init-hooks", "--project-root", str(project_root),
    ])
    assert code == 0

    settings = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    ups_block = settings["hooks"]["UserPromptSubmit"]
    # Flatten the nested hooks structure to a list of commands
    cmds = []
    for entry in ups_block:
        for h in entry.get("hooks", []):
            cmds.append(h.get("command", ""))
    composer_cmds = [c for c in cmds if "emit-prompt-context" in c]
    assert len(composer_cmds) == 1, (
        f"expected exactly one emit-prompt-context hook, got {cmds!r}"
    )
    # Must include the managed marker so future --force can identify it.
    assert "--__cr-managed" in composer_cmds[0]


def test_init_hooks_uses_binary_when_inject_interventions_false(
    tmp_path, monkeypatch,
):
    """Default behavior unchanged: inject_interventions=false (default)
    preserves the binary fast-path on platforms where it's available, or
    the .ps1/.sh fallback otherwise. Specifically must NOT emit
    `emit-prompt-context`."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    code = cli.main([
        "init-hooks", "--project-root", str(project_root),
    ])
    assert code == 0

    settings = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    ups_block = settings["hooks"]["UserPromptSubmit"]
    cmds = []
    for entry in ups_block:
        for h in entry.get("hooks", []):
            cmds.append(h.get("command", ""))
    composer_cmds = [c for c in cmds if "emit-prompt-context" in c]
    assert not composer_cmds, (
        f"emit-prompt-context should not be used when inject_interventions=false: {cmds!r}"
    )
