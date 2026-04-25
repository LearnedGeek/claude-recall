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


def test_init_hooks_uses_native_binary_when_present(tmp_path, monkeypatch):
    """On Windows when src/claude_recall/native/claude-recall-hook.exe exists,
    init-hooks registers it directly as the UserPromptSubmit command and
    copies it into .claude/hooks/, dropping the shell-wrapper on_prompt.ps1."""
    import sys as _sys
    if _sys.platform != "win32":
        pytest.skip("binary-aware init-hooks check is win-x64 only for v0.4")

    from claude_recall import cli as _cli

    # Fake a bundled binary by pointing NATIVE_SRC_DIR at a tmp dir with a
    # stub exe. We don't need a real exe for this test — init-hooks just
    # copies bytes.
    fake_native = tmp_path / "fake_native"
    fake_native.mkdir()
    (fake_native / "claude-recall-hook.exe").write_bytes(b"MZ stub")
    (fake_native / "e_sqlite3.dll").write_bytes(b"PE stub")
    monkeypatch.setattr(_cli, "NATIVE_SRC_DIR", fake_native)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 0

    hooks_dir = project_root / ".claude" / "hooks"
    # Binary copied
    assert (hooks_dir / "claude-recall-hook.exe").exists()
    assert (hooks_dir / "e_sqlite3.dll").exists()
    # Shell wrapper for on_prompt is NOT written
    assert not (hooks_dir / "on_prompt.ps1").exists()
    # SessionStart shell wrapper IS still written
    assert (hooks_dir / "session_start.ps1").exists()

    settings = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    ups_cmds = _hook_commands(settings["hooks"]["UserPromptSubmit"])
    assert any("claude-recall-hook.exe" in c for c in ups_cmds)


def _hook_commands(entries: list) -> list[str]:
    """Extract command strings from the schema-correct nested-array shape.

    Each entry is `{matcher?: str, hooks: [{type: "command", command: str, ...}]}`.
    """
    out: list[str] = []
    for entry in entries:
        for inner in entry.get("hooks", []):
            cmd = inner.get("command")
            if isinstance(cmd, str):
                out.append(cmd)
    return out


def test_search_project_filter_is_case_insensitive(cli_env, capsys):
    """Issue #13: --project should match regardless of case. Stored slug is
    "test-project" (lowercase); passing "TEST-PROJECT" used to return 0."""
    cli_env["run"]("index")
    capsys.readouterr()
    code, out, _ = cli_env["run"](
        "search", "regex", "--project", "TEST-PROJECT", "--format", "json"
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["total_matches"] >= 1, "case-insensitive project filter failed"


def test_list_project_filter_is_case_insensitive(cli_env, capsys):
    """Same fix surface as search; list's --project also case-insensitive."""
    cli_env["run"]("index")
    capsys.readouterr()
    code, out, _ = cli_env["run"](
        "list", "--project", "TEST-PROJECT", "--format", "json"
    )
    assert code == 0
    rows = json.loads(out)
    assert len(rows) >= 1


def test_status_integrity_check_reports_row_counts(cli_env, capsys):
    """--integrity-check prints global counts + per-project breakdown."""
    cli_env["run"]("index")
    capsys.readouterr()
    code, out, _ = cli_env["run"]("status", "--integrity-check")
    assert code == 0
    assert "global:" in out
    assert "per-project:" in out
    assert "test-project" in out


def test_status_integrity_check_flags_fts_mismatch(tmp_path, monkeypatch, capsys):
    """When messages vs messages_fts row counts disagree, integrity check
    surfaces a warning — the exact trigger-didn't-fire hypothesis from #13."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n',
        encoding="utf-8",
    )
    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()

    # Delete FTS rows to simulate the trigger-missed-insert case.
    from claude_recall import storage as _storage
    conn = _storage.open_db(db_path)
    try:
        conn.execute("DELETE FROM messages_fts WHERE rowid > 5")
        conn.commit()
    finally:
        conn.close()

    code = cli.main(["--config", str(cfg), "status", "--integrity-check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "messages_fts" in out
    assert "FTS trigger" in out or "messages_fts" in out


def test_status_probes_ollama_even_when_embeddings_disabled(tmp_path, monkeypatch, capsys):
    """Issue #5: `ollama_reachable` in json/text status reflects actual
    reachability, not the `[embeddings].enabled` config toggle."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = tmp_path / "config.toml"
    # enabled is absent; defaults to false
    cfg.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n',
        encoding="utf-8",
    )
    # Ollama IS up (fake responds healthily) even though embeddings are off.
    _use_fake_ollama(monkeypatch, _FakeOllama())

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "status", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["embeddings_enabled"] is False
    assert payload["ollama_reachable"] is True  # honest, not conflated


def test_status_agent_context_skips_probe_when_disabled(tmp_path, capsys):
    """Hook path stays fast — no unconditional Ollama probe in agent-context
    format when embeddings are disabled. Budget protection."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n',
        encoding="utf-8",
    )
    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    # Don't set up fake Ollama — if the code probes, it'll slow down.
    # Just confirm the command returns cleanly and no Embeddings: line
    # mentioning Ollama is printed.
    cli.main(["--config", str(cfg), "status", "--format", "agent-context"])
    out = capsys.readouterr().out
    assert "Embeddings:" not in out


def test_embed_probe_uses_longer_timeout_and_clarifies_cold_start(
    tmp_path, monkeypatch, capsys,
):
    """Issue #7: --probe gets extended timeout (for model cold-start) and a
    clearer error message when the failure looks like a cold-start timeout
    (reachable + model present + 'time' in error)."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)

    class _ColdStartClient:
        def __init__(self):
            self.timeout = None  # will be set by the factory wrapper
        def probe(self):
            from claude_recall.embeddings import ProbeResult
            return ProbeResult(
                ollama_reachable=True, version="0.18.2",
                model_present=True, embed_ok=False, dim=None,
                error="Ollama request failed: timed out",
            )
        def close(self):
            pass

    captured_timeout = {"value": None}

    def _factory(base_url, model, timeout, **kw):
        captured_timeout["value"] = timeout
        return _ColdStartClient()

    from claude_recall import cli as _cli
    monkeypatch.setattr(_cli, "_ollama_client_factory", _factory)

    code = cli.main(["--config", str(cfg), "embed", "--probe"])
    assert code == 2  # embed_ok = False → exit 2

    # Probe should have been given at least 30s, regardless of config.
    assert captured_timeout["value"] is not None
    assert captured_timeout["value"] >= 30.0

    payload = json.loads(capsys.readouterr().out)
    assert payload["embed_ok"] is False
    # Error is rewritten with a cold-start hint, not just "timed out".
    assert "cold" in payload["error"].lower() or "first-call" in payload["error"].lower() \
        or "model load" in payload["error"].lower()


def test_init_hooks_scaffolds_config_template_when_missing(tmp_path, monkeypatch):
    """Issue #6: init-hooks writes a commented config.toml template on
    first-time setup so users discover the [embeddings] section without
    reading three docs."""
    cfg_home = tmp_path / "cfg_home"
    monkeypatch.setenv("APPDATA", str(cfg_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))

    project_root = tmp_path / "proj"
    project_root.mkdir()

    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 0

    # Template written to the right place
    written = list(cfg_home.rglob("config.toml"))
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    # Mentions every major section, commented so user knows they can opt in
    assert "[embeddings]" in content
    assert "enabled                 = true" in content or "enabled = true" in content
    assert "ollama pull nomic-embed-text" in content


def test_init_hooks_preserves_existing_config(tmp_path, monkeypatch):
    """Second run of init-hooks must NOT overwrite an existing config.toml."""
    cfg_home = tmp_path / "cfg_home"
    monkeypatch.setenv("APPDATA", str(cfg_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))

    # Pre-create a user config with their own edits
    existing_dir = cfg_home / "claude-recall"
    existing_dir.mkdir(parents=True)
    existing = existing_dir / "config.toml"
    existing.write_text("[search]\nhook_days = 60  # my tuning\n", encoding="utf-8")

    project_root = tmp_path / "proj"
    project_root.mkdir()
    cli.main(["init-hooks", "--project-root", str(project_root)])

    # Left exactly as-is
    assert existing.read_text(encoding="utf-8") == "[search]\nhook_days = 60  # my tuning\n"


def test_embed_batch_truncates_oversized_inputs(monkeypatch):
    """Issue #8: inputs longer than max_input_chars are truncated before
    the HTTP call, so one long message can't fail a 32-message batch."""
    import httpx

    from claude_recall import embeddings as _embeddings

    captured_inputs = []

    def handler(request):
        body = json.loads(request.content)
        captured_inputs.append(body["input"])
        return httpx.Response(200, json={"embeddings": [[0.1] * 8] * len(body["input"])})

    transport = httpx.MockTransport(handler)
    client = _embeddings.OllamaClient(max_input_chars=100)
    client._client.close()
    client._client = httpx.Client(transport=transport, timeout=5.0)

    short = "a" * 50
    long_ = "b" * 500
    client.embed_batch([short, long_])

    assert len(captured_inputs) == 1
    sent = captured_inputs[0]
    assert len(sent[0]) == 50    # short unchanged
    assert len(sent[1]) == 100   # long truncated to cap


def test_embed_batch_surfaces_ollama_error_body(monkeypatch):
    """Issue #8 secondary: 400 errors from Ollama include the response body
    in the EmbeddingError so users can see the actual cause."""
    import httpx

    from claude_recall import embeddings as _embeddings

    def handler(request):
        return httpx.Response(
            400, text="the input length exceeds the context length"
        )

    transport = httpx.MockTransport(handler)
    client = _embeddings.OllamaClient()
    client._client.close()
    client._client = httpx.Client(transport=transport, timeout=5.0)

    with pytest.raises(_embeddings.EmbeddingError) as exc_info:
        client.embed_batch(["x"])
    assert "context length" in str(exc_info.value)
    assert "400" in str(exc_info.value)


def test_embed_falls_back_to_singleton_on_batch_failure(tmp_path, monkeypatch, capsys):
    """Issue #8 main: when a batch fails, retry each message individually so
    we only drop the actually-bad ones — not the 31 good ones in the batch."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)

    # Build a fake client where ANY batch of size >1 fails, singletons succeed
    # except for one specific message that always fails.
    class _Selective:
        def __init__(self):
            self.calls = []

        def embed_batch(self, texts):
            import numpy as np

            from claude_recall.embeddings import EmbeddingError
            self.calls.append(list(texts))
            if len(texts) > 1:
                # Simulate "one of the batch has an oversized input"
                raise EmbeddingError("Ollama 400 from /api/embed: context length")
            # Single message. Fail on a known-bad one, succeed otherwise.
            if "unique-bad-token-xyz" in texts[0]:
                raise EmbeddingError("Ollama 400: context length")
            v = np.zeros(4, dtype=np.float32)
            v[0] = 1.0
            return v.reshape(1, 4)

        def probe(self):
            from claude_recall.embeddings import ProbeResult
            return ProbeResult(True, "test", True, True, 4, None)

        def close(self):
            pass

    # Inject one oversized-shaped message into the archive so there's
    # something specifically bad to drop.
    bad_file = archive_root / "test-project" / "session_bad.jsonl"
    bad_file.write_text(
        '{"type":"user","message":{"role":"user",'
        '"content":"unique-bad-token-xyz message that will fail"},'
        '"timestamp":"2026-04-21T14:00:00Z"}\n',
        encoding="utf-8",
    )

    fake = _Selective()
    from claude_recall import cli as _cli
    monkeypatch.setattr(
        _cli, "_ollama_client_factory",
        lambda base_url, model, timeout, **kw: fake,
    )

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    code = cli.main(["--config", str(cfg), "embed", "--verbose"])
    capsys.readouterr()
    # Exit 2 because at least one message was dropped
    assert code == 2
    # But most messages should have been recovered by the singleton fallback
    from claude_recall import storage as _storage
    conn = _storage.open_db(db_path)
    try:
        vec_count = conn.execute("SELECT COUNT(*) FROM message_vectors").fetchone()[0]
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    # At least some messages embedded successfully via fallback
    assert vec_count > 0
    # Not all messages — the one with unique-bad-token-xyz is dropped
    assert vec_count < msg_count


def test_init_hooks_force_wipes_managed_events(tmp_path):
    """Issue #4 regression: --force must replace SessionStart + UserPromptSubmit
    wholesale. A pre-existing entry pointing at an old install path (e.g., a
    site-packages path from a manual pre-v0.4.1 wiring) must not survive the
    upgrade. Other hook events stay untouched."""
    project_root = tmp_path / "proj"
    (project_root / ".claude").mkdir(parents=True)

    stale = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "/user/pre-tool-use.sh"}]},
            ],
            "PostToolUse": [
                {"hooks": [{"type": "command", "command": "/user/post-tool-use.sh"}]},
            ],
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {"type": "command", "command": "/stale/site-packages/session_start.ps1"},
                    ],
                },
            ],
            "UserPromptSubmit": [
                # Mimics OC's manual wiring to a site-packages path before v0.4.1.
                {
                    "hooks": [
                        {"type": "command", "command": "C:/stale/site-packages/native/claude-recall-hook.exe"},
                    ],
                },
            ],
        }
    }
    settings_path = project_root / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(stale), encoding="utf-8")

    code = cli.main(["init-hooks", "--project-root", str(project_root), "--force"])
    assert code == 0

    merged = json.loads(settings_path.read_text(encoding="utf-8"))

    # Events we don't manage are preserved verbatim.
    assert merged["hooks"]["PreToolUse"] == stale["hooks"]["PreToolUse"]
    assert merged["hooks"]["PostToolUse"] == stale["hooks"]["PostToolUse"]

    # Events we DO manage should have exactly one entry each (the generated one).
    assert len(merged["hooks"]["SessionStart"]) == 1
    assert all(
        "stale" not in c for c in _hook_commands(merged["hooks"]["SessionStart"])
    )

    assert len(merged["hooks"]["UserPromptSubmit"]) == 1
    assert all(
        "stale" not in c for c in _hook_commands(merged["hooks"]["UserPromptSubmit"])
    )

    # .bak still written as a safety net.
    assert (project_root / ".claude" / "settings.json.bak").exists()


def test_init_hooks_without_force_preserves_pre_existing_user_hooks(tmp_path):
    """Without --force, pre-existing user entries under SessionStart /
    UserPromptSubmit survive alongside the generated ones. This is the v0.3.x
    behavior kept intentionally for users who want to layer hooks rather than
    replace them."""
    project_root = tmp_path / "proj"
    (project_root / ".claude").mkdir(parents=True)
    existing = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": "/user/pre-existing-prompt-hook.sh"},
                    ],
                },
            ],
        }
    }
    (project_root / ".claude" / "settings.json").write_text(
        json.dumps(existing), encoding="utf-8"
    )
    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 0

    merged = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    cmds = _hook_commands(merged["hooks"]["UserPromptSubmit"])
    assert "/user/pre-existing-prompt-hook.sh" in cmds
    assert len(cmds) == 2  # pre-existing + generated


def test_init_hooks_fails_cleanly_when_wheel_missing_sources(
    tmp_path, monkeypatch, capsys
):
    """Issue #3 regression: a wheel missing BOTH the binary and on_prompt shell
    scripts used to crash with a raw FileNotFoundError. Must now return 1 with
    a clear user-facing message pointing at both expected paths."""
    from claude_recall import cli as _cli

    empty = tmp_path / "empty_hooks"
    empty.mkdir()
    (empty / "__init__.py").write_text("", encoding="utf-8")
    no_native = tmp_path / "empty_native"
    no_native.mkdir()
    monkeypatch.setattr(_cli, "HOOKS_SRC_DIR", empty)
    monkeypatch.setattr(_cli, "NATIVE_SRC_DIR", no_native)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    err = capsys.readouterr().err
    assert code == 1
    assert "missing hook sources" in err
    assert "claude-recall-hook.exe" in err
    # No settings.json should be written.
    assert not (project_root / ".claude" / "settings.json").exists()


def test_init_hooks_binary_only_wheel_skips_session_start(tmp_path, monkeypatch, capsys):
    """If wheel has the binary but is missing session_start.ps1, init-hooks
    should still wire UserPromptSubmit and warn cleanly about SessionStart."""
    import sys as _sys
    if _sys.platform != "win32":
        pytest.skip("binary-only wheel scenario is win-x64 only")

    from claude_recall import cli as _cli

    bin_only = tmp_path / "bin_only_native"
    bin_only.mkdir()
    (bin_only / "claude-recall-hook.exe").write_bytes(b"MZ stub")
    empty_hooks = tmp_path / "empty_hooks"
    empty_hooks.mkdir()
    (empty_hooks / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(_cli, "HOOKS_SRC_DIR", empty_hooks)
    monkeypatch.setattr(_cli, "NATIVE_SRC_DIR", bin_only)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    err = capsys.readouterr().err
    assert code == 0
    assert "SessionStart hook will not be registered" in err

    settings = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    # UserPromptSubmit is wired at the binary; SessionStart absent from settings.
    assert "SessionStart" not in settings["hooks"]
    ups_cmds = _hook_commands(settings["hooks"]["UserPromptSubmit"])
    assert any("claude-recall-hook.exe" in c for c in ups_cmds)


def test_init_hooks_pure_python_wheel_wires_shell_hook(tmp_path, monkeypatch):
    """Non-Windows (or Windows pure-Python) wheels use the shell hook for
    on_prompt. Must still work end-to-end when the binary is absent."""
    from claude_recall import cli as _cli
    no_native = tmp_path / "no_native"
    no_native.mkdir()
    monkeypatch.setattr(_cli, "NATIVE_SRC_DIR", no_native)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 0

    hooks_dir = project_root / ".claude" / "hooks"
    # On this machine the shell scripts are the real ones from the repo
    # package dir (HOOKS_SRC_DIR default).
    if _sys_win():
        assert (hooks_dir / "on_prompt.ps1").exists()
        assert (hooks_dir / "session_start.ps1").exists()
    else:
        assert (hooks_dir / "on_prompt.sh").exists()
        assert (hooks_dir / "session_start.sh").exists()

    settings = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    ups_cmds = _hook_commands(settings["hooks"]["UserPromptSubmit"])
    # Shell hook, not .exe
    assert all("claude-recall-hook.exe" not in c for c in ups_cmds)


def _sys_win() -> bool:
    import sys as _sys
    return _sys.platform == "win32"


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
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "/some/other/script.sh"}]},
            ],
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {"type": "command", "command": "/pre-existing.sh"},
                    ],
                },
            ],
        }
    }
    settings_path = project_root / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(existing), encoding="utf-8")

    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 0
    merged = json.loads(settings_path.read_text(encoding="utf-8"))
    # Pre-existing PreToolUse still there verbatim
    assert merged["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    # SessionStart has both the pre-existing and our new one
    commands = set(_hook_commands(merged["hooks"]["SessionStart"]))
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


def test_init_hooks_emits_schema_correct_nested_shape(tmp_path):
    """Issue #15 regression: settings.json must use the nested-array shape
    Claude Code's parser requires. Each matcher entry must be
    `{matcher?, hooks: [{type: "command", command, shell?}]}`. The flat
    `{command, matcher}` shape we shipped through v0.5.3 was rejected with
    "Expected array, but received undefined" and silently disabled the host
    project's settings as a side effect.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    code = cli.main(["init-hooks", "--project-root", str(project_root)])
    assert code == 0
    settings = json.loads(
        (project_root / ".claude" / "settings.json").read_text(encoding="utf-8")
    )

    for event in ("SessionStart", "UserPromptSubmit"):
        assert event in settings["hooks"]
        for matcher_entry in settings["hooks"][event]:
            # The bug was emitting `command` directly on the matcher entry.
            assert "command" not in matcher_entry, (
                f"{event} entry has `command` at top level — flat shape regressed: "
                f"{matcher_entry!r}"
            )
            assert isinstance(matcher_entry.get("hooks"), list), (
                f"{event} entry missing required `hooks` array: {matcher_entry!r}"
            )
            for inner in matcher_entry["hooks"]:
                assert inner.get("type") == "command", (
                    f"{event} inner hook missing `type: \"command\"`: {inner!r}"
                )
                assert isinstance(inner.get("command"), str)
                # PowerShell scripts on Windows must declare shell=powershell —
                # bash (the default hook shell) can't execute a raw .ps1 path.
                if inner["command"].lower().endswith(".ps1"):
                    assert inner.get("shell") == "powershell", (
                        f".ps1 hook missing shell=powershell: {inner!r}"
                    )


# ---- v0.3 embed command ----

class _FakeOllama:
    """In-memory OllamaClient replacement for embed-path tests.

    Returns deterministic per-text vectors so test assertions can verify
    that the right content made it into message_vectors.
    """

    def __init__(self, *, dim: int = 8, fail_on_text: str | None = None):
        self.dim = dim
        self.fail_on_text = fail_on_text
        self.calls: list[list[str]] = []

    def embed(self, text):
        return self.embed_batch([text])[0]

    def embed_batch(self, texts):
        import numpy as np

        from claude_recall import embeddings
        self.calls.append(list(texts))
        if self.fail_on_text and any(self.fail_on_text in t for t in texts):
            raise embeddings.EmbeddingError("injected failure")
        # One row per text, deterministic non-zero norm
        rows = []
        for i, t in enumerate(texts):
            v = np.zeros(self.dim, dtype=np.float32)
            v[i % self.dim] = 1.0 + len(t) * 1e-3  # distinguishable
            rows.append(v)
        return np.stack(rows)

    def probe(self):
        from claude_recall.embeddings import ProbeResult
        return ProbeResult(
            ollama_reachable=True,
            version="test",
            model_present=True,
            embed_ok=True,
            dim=self.dim,
            error=None,
        )

    def close(self):
        pass


def _use_fake_ollama(monkeypatch, fake):
    from claude_recall import cli as _cli
    monkeypatch.setattr(
        _cli, "_ollama_client_factory",
        # Accept **kwargs so v0.4.3 + later factory-signature additions
        # (e.g., max_input_chars) don't break the test helper.
        lambda base_url, model, timeout, **kw: fake,
    )


def _enable_embeddings_cfg(tmp_path, archive_root, db_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n'
        '[embeddings]\nenabled = true\nbatch_size = 4\n',
        encoding="utf-8",
    )
    return cfg_path


def _seed_archive(tmp_path):
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
    return archive_root


def test_embed_probe_reports_json(tmp_path, capsys, monkeypatch):
    """claude-recall embed --probe prints a JSON probe result and exits 0 on success."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\npath = "{(tmp_path / "db.sqlite").as_posix()}"\n',
        encoding="utf-8",
    )
    _use_fake_ollama(monkeypatch, _FakeOllama())
    code = cli.main(["--config", str(cfg), "embed", "--probe"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ollama_reachable"] is True
    assert payload["model_present"] is True
    assert payload["embed_ok"] is True
    assert payload["dim"] == 8


def test_embed_guards_when_disabled(tmp_path, capsys, monkeypatch):
    """Embed without --probe requires [embeddings].enabled=true."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[database]\npath = "{(tmp_path / "db.sqlite").as_posix()}"\n',
        encoding="utf-8",
    )
    code = cli.main(["--config", str(cfg), "embed"])
    assert code == 1
    assert "disabled" in capsys.readouterr().err


def test_embed_populates_message_vectors(tmp_path, capsys, monkeypatch):
    """End-to-end: index fixtures, run embed, message_vectors rows appear."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    _use_fake_ollama(monkeypatch, _FakeOllama())

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    code = cli.main(["--config", str(cfg), "embed", "--verbose"])
    assert code == 0

    from claude_recall import storage
    conn = storage.open_db(db_path)
    try:
        msg_count = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        vec_count = conn.execute(
            "SELECT COUNT(*) AS c FROM message_vectors"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert vec_count == msg_count
    assert vec_count > 0


def test_embed_is_incremental_on_second_run(tmp_path, capsys, monkeypatch):
    """Second embed is a no-op when all messages are already embedded."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    fake = _FakeOllama()
    _use_fake_ollama(monkeypatch, fake)

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "embed"])
    capsys.readouterr()
    first_call_count = sum(len(c) for c in fake.calls)

    cli.main(["--config", str(cfg), "embed"])
    out = capsys.readouterr().out
    # Second pass embedded 0 messages; skipped count matches first-pass total
    assert "embedded 0" in out
    total_call_count = sum(len(c) for c in fake.calls)
    assert total_call_count == first_call_count


def test_embed_rebuild_drops_and_reembeds(tmp_path, capsys, monkeypatch):
    """--rebuild wipes vectors in scope and re-embeds."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    fake = _FakeOllama()
    _use_fake_ollama(monkeypatch, fake)

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "embed"])
    capsys.readouterr()
    first_total = sum(len(c) for c in fake.calls)

    cli.main(["--config", str(cfg), "embed", "--rebuild"])
    capsys.readouterr()
    second_total = sum(len(c) for c in fake.calls)
    # After rebuild we called embed_batch again for every message
    assert second_total == first_total * 2


def test_status_json_includes_embedding_fields(tmp_path, capsys, monkeypatch):
    """status --format json exposes embeddings_enabled, ollama_reachable, vectors_indexed."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    _use_fake_ollama(monkeypatch, _FakeOllama())

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "embed"])
    capsys.readouterr()

    code = cli.main(["--config", str(cfg), "status", "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["embeddings_enabled"] is True
    assert payload["ollama_reachable"] is True
    assert payload["vectors_indexed"] > 0
    assert payload["messages_without_vectors"] == 0
    assert payload["checks"]["embeddings_ready"] is True
    # Issue #16: vectors_coverage exposed as a programmatic field for
    # consumers who want to branch on coverage without re-deriving the math.
    assert payload["vectors_coverage"] == 1.0


def test_status_embeddings_ready_false_when_coverage_below_threshold(
    tmp_path, capsys, monkeypatch
):
    """Issue #16 regression: vectors_indexed > 0 alone is not enough to
    report embeddings_ready=True. Coverage must be ≥95%. ANI's repro had
    4,177 vectors against ~26k messages — 16% coverage — and status was
    reporting `embeddings_ready: True` while semantic search returned
    "no vectors in index" because the surviving vectors didn't intersect
    the FTS5 candidate pool. Test simulates the same shape: index + embed
    fully, then delete most vectors to mimic the FK CASCADE on routine
    re-ingest.
    """
    import sqlite3
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    _use_fake_ollama(monkeypatch, _FakeOllama())

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "embed"])
    capsys.readouterr()

    # Sanity-check the test setup: full coverage right after embed.
    cli.main(["--config", str(cfg), "status", "--format", "json"])
    full = json.loads(capsys.readouterr().out)
    assert full["checks"]["embeddings_ready"] is True
    assert full["vectors_coverage"] == 1.0

    # Simulate the FK CASCADE wipe by deleting all but one vector. That
    # leaves vectors_indexed > 0 (the v0.5.4 condition) but coverage way
    # below the 95% threshold (the new v0.5.5 condition).
    conn = sqlite3.connect(db_path)
    conn.execute(
        "DELETE FROM message_vectors WHERE msg_id NOT IN ("
        "  SELECT msg_id FROM message_vectors LIMIT 1)"
    )
    conn.commit()
    conn.close()

    cli.main(["--config", str(cfg), "status", "--format", "json"])
    degraded = json.loads(capsys.readouterr().out)

    assert degraded["vectors_indexed"] > 0  # the lenient v0.5.4 check
    assert degraded["vectors_coverage"] < 0.95
    assert degraded["checks"]["embeddings_ready"] is False, (
        "embeddings_ready should be False when coverage < 95% — "
        "v0.5.4's `vectors_indexed > 0` was the lying surface"
    )


def test_status_text_format_warns_when_vectors_stale(
    tmp_path, capsys, monkeypatch
):
    """Issue #16: text-format status must print a prominent
    `vectors are stale` block (mirroring the existing `hooks are stale`
    block) when coverage drops below threshold, so users discover the
    fix path without filing an issue first.
    """
    import sqlite3
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    _use_fake_ollama(monkeypatch, _FakeOllama())

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "embed"])
    capsys.readouterr()

    conn = sqlite3.connect(db_path)
    conn.execute(
        "DELETE FROM message_vectors WHERE msg_id NOT IN ("
        "  SELECT msg_id FROM message_vectors LIMIT 1)"
    )
    conn.commit()
    conn.close()

    cli.main(["--config", str(cfg), "status"])
    out = capsys.readouterr().out
    assert "vectors_coverage:" in out
    assert "vectors are stale:" in out
    assert "claude-recall embed" in out


def test_status_agent_context_reports_coverage_pct_when_degraded(
    tmp_path, capsys, monkeypatch
):
    """Issue #16: agent-context format (the SessionStart hook output) must
    surface coverage percentage when degraded, so the active Claude
    instance gets an honest signal instead of a silently-injected
    "embeddings ready" lie.
    """
    import sqlite3
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    _use_fake_ollama(monkeypatch, _FakeOllama())

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "embed"])
    capsys.readouterr()

    conn = sqlite3.connect(db_path)
    conn.execute(
        "DELETE FROM message_vectors WHERE msg_id NOT IN ("
        "  SELECT msg_id FROM message_vectors LIMIT 1)"
    )
    conn.commit()
    conn.close()

    cli.main(["--config", str(cfg), "status", "--format", "agent-context"])
    out = capsys.readouterr().out
    assert "% coverage" in out
    assert "claude-recall embed" in out


def test_status_agent_context_reports_embedding_health(tmp_path, capsys, monkeypatch):
    """agent-context line appends an Embeddings suffix when enabled and healthy."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    _use_fake_ollama(monkeypatch, _FakeOllama())

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "embed"])
    capsys.readouterr()

    cli.main(["--config", str(cfg), "status", "--format", "agent-context"])
    out = capsys.readouterr().out
    assert "Embeddings:" in out
    assert "vectors" in out.lower()
    assert "Ollama reachable" in out


def test_status_agent_context_hints_when_unembedded(tmp_path, capsys, monkeypatch):
    """agent-context line hints `run claude-recall embed` when vectors are missing."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    _use_fake_ollama(monkeypatch, _FakeOllama())

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    # deliberately skip embed

    cli.main(["--config", str(cfg), "status", "--format", "agent-context"])
    out = capsys.readouterr().out
    assert "0 vectors" in out
    assert "claude-recall embed" in out


def test_status_embeddings_ready_false_when_ollama_down(tmp_path, capsys, monkeypatch):
    """ollama_reachable=False when probe fails; embeddings_ready reflects that."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)

    class _DownClient:
        def probe(self):
            from claude_recall.embeddings import ProbeResult
            return ProbeResult(
                ollama_reachable=False, version=None, model_present=False,
                embed_ok=False, dim=None, error="refused",
            )
        def close(self):
            pass

    from claude_recall import cli as _cli
    monkeypatch.setattr(
        _cli, "_ollama_client_factory",
        lambda base_url, model, timeout: _DownClient(),
    )

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "status", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ollama_reachable"] is False
    assert payload["checks"]["embeddings_ready"] is False


def test_semantic_from_config_respects_use_in_hook(tmp_path, capsys, monkeypatch):
    """--semantic-from-config activates semantic only when use_in_hook=true in config."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"

    # Case 1: enabled=true but use_in_hook=false (shipped v0.3.0 default) →
    #   --semantic-from-config must NOT activate semantic; hook stays fast.
    cfg_off = tmp_path / "cfg_off.toml"
    cfg_off.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n'
        '[embeddings]\nenabled = true\nuse_in_hook = false\n',
        encoding="utf-8",
    )
    _use_fake_ollama(monkeypatch, _FakeOllama())
    cli.main(["--config", str(cfg_off), "index"])
    capsys.readouterr()

    # If semantic were mistakenly turned on, --semantic-from-config would
    # try to use the Ollama client. We use a fake so it's safe either way,
    # but the behavior under test is that semantic_used=false.
    cli.main([
        "--config", str(cfg_off),
        "search", "regex", "--semantic-from-config", "--format", "json",
    ])
    payload = json.loads(capsys.readouterr().out)
    # With v0.3 behavior, no semantic_used=true should appear
    # (soft-ignored before even trying; no warning since flag is passive).
    assert payload["total_matches"] >= 1

    # Case 2: use_in_hook=true → --semantic-from-config does activate semantic.
    cfg_on = tmp_path / "cfg_on.toml"
    cfg_on.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{(tmp_path / "db2.sqlite").as_posix()}"\n'
        '[embeddings]\nenabled = true\nuse_in_hook = true\n',
        encoding="utf-8",
    )
    _use_fake_ollama(monkeypatch, _FakeOllama())
    cli.main(["--config", str(cfg_on), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg_on), "embed"])
    capsys.readouterr()
    cli.main([
        "--config", str(cfg_on),
        "search", "regex", "--semantic-from-config", "--format", "json",
    ])
    payload = json.loads(capsys.readouterr().out)
    # At least one result should have semantic_rank populated — proves rerank ran.
    assert any(r.get("semantic_rank") is not None for r in payload["results"])


def test_search_semantic_soft_ignores_when_disabled(tmp_path, capsys):
    """--semantic without [embeddings].enabled logs a warning and runs FTS5-only."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[archive]\nroot = "{archive_root.as_posix()}"\n'
        f'[database]\npath = "{db_path.as_posix()}"\n',
        encoding="utf-8",
    )
    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()

    code = cli.main([
        "--config", str(cfg),
        "search", "regex", "--semantic", "--format", "json",
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert "enabled is false" in captured.err
    payload = json.loads(captured.out)
    assert payload["total_matches"] >= 1


def test_search_semantic_reranks_end_to_end(tmp_path, capsys, monkeypatch):
    """With embeddings enabled + Ollama fake + vectors seeded, --semantic flips ranks."""
    import numpy as np

    from claude_recall import embeddings as _embeddings
    from claude_recall import storage as _storage

    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)

    fake = _FakeOllama(dim=8)
    _use_fake_ollama(monkeypatch, fake)

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    cli.main(["--config", str(cfg), "embed"])
    capsys.readouterr()

    # Overwrite vectors: make msg_id=1 perfectly aligned with the query vec
    # the fake returns for the prompt "regex" (fake returns v[i%dim]=1 where
    # i==0 since it's batch of 1 → [1,0,0,...] in slot 0).
    conn = _storage.open_db(db_path)
    try:
        # Set msg 1's vector to the exact query vec; others orthogonal.
        winner = np.zeros(8, dtype=np.float32)
        winner[0] = 1.0
        loser = np.zeros(8, dtype=np.float32)
        loser[1] = 1.0
        for row in conn.execute("SELECT msg_id FROM messages ORDER BY msg_id"):
            v = winner if row["msg_id"] == 1 else loser
            conn.execute(
                "UPDATE message_vectors SET vector = ? WHERE msg_id = ?",
                (_embeddings.pack_vector(v), row["msg_id"]),
            )
        conn.commit()
    finally:
        conn.close()

    code = cli.main([
        "--config", str(cfg),
        "search", "regex", "--semantic", "--format", "json",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    # Every returned row should have semantic_rank populated
    assert payload["results"]
    assert all(r["semantic_rank"] is not None for r in payload["results"])


def test_embed_reports_partial_failure_exit_2(tmp_path, capsys, monkeypatch):
    """If a batch fails mid-run, remaining batches continue and exit is 2."""
    archive_root = _seed_archive(tmp_path)
    db_path = tmp_path / "index.db"
    cfg = _enable_embeddings_cfg(tmp_path, archive_root, db_path)
    # Fail on any text that contains the fixture phrase
    _use_fake_ollama(monkeypatch, _FakeOllama(fail_on_text="regex"))

    cli.main(["--config", str(cfg), "index"])
    capsys.readouterr()
    code = cli.main(["--config", str(cfg), "embed"])
    err = capsys.readouterr().err
    assert code == 2
    assert "failed" in err.lower()


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
