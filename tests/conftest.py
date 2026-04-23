"""Pytest fixtures for claude-recall tests.

Covers:
- Temporary archive dir with sample .jsonl fixtures
- Temporary SQLite DB path
- Opened DB connection with schema installed
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from claude_recall import storage

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
