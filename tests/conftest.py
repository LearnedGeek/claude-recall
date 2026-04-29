"""Pytest fixtures for claude-recall tests.

Covers:
- Temporary archive dir with sample .jsonl fixtures
- Temporary SQLite DB path
- Opened DB connection with schema installed
- Auto-isolated config so tests don't pick up the developer's real
  ~/.config/claude-recall/config.toml (would surface as e.g. unexpected
  `[hooks].inject_time = true` leaking into tests that don't pass --config)
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from claude_recall import storage

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_config_from_dev_machine(monkeypatch):
    """Force `load_config(None)` to return dataclass defaults during tests
    that don't pass an explicit config path. Prevents the developer's real
    config.toml (e.g., one with `[hooks].inject_time = true` set) from
    leaking into test runs that assume default-off behavior.

    Tests that DO pass `--config <path>` still go through the real loader
    against that explicit path, so config-loading behavior itself remains
    testable. Only the implicit-default path is short-circuited.

    Tests that need to verify `default_config_path` itself (e.g.,
    test_default_paths_platform_correct) call it directly and aren't
    affected by this fixture.
    """
    from claude_recall import config as _config
    original = _config.load_config

    def isolated_load(path=None):
        if path is None:
            return _config.Config()
        return original(path)

    monkeypatch.setattr(_config, "load_config", isolated_load)
    # Also patch the imported reference in cli.py since _cmd_* call it via
    # the cli-module-level symbol, not via _config.load_config attribute.
    from claude_recall import cli as _cli
    monkeypatch.setattr(_cli, "load_config", isolated_load)
    yield


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    """Temporary archive root with a single project subdir containing sample fixtures.

    Structure:
        tmp_path/projects/test-project/session-short.jsonl
        tmp_path/projects/test-project/session-malformed.jsonl
        tmp_path/projects/test-project/session-tool-blocks.jsonl
    """
    root = tmp_path / "projects"
    project = root / "test-project"
    project.mkdir(parents=True)
    for name in (
        "session_short.jsonl",
        "session_malformed.jsonl",
        "session_tool_blocks.jsonl",
    ):
        shutil.copy(FIXTURES_DIR / name, project / name)
    return root


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Path to a SQLite DB that does not yet exist — storage.open_db() will create it."""
    return tmp_path / "test_index.db"


@pytest.fixture
def db_conn(temp_db: Path) -> sqlite3.Connection:
    """Opened, schema-initialized DB connection."""
    conn = storage.open_db(temp_db)
    yield conn
    conn.close()
