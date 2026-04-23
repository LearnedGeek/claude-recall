"""Pytest fixtures for claude-recall tests.

Covers:
- Temporary archive dir with sample .jsonl fixtures
- Temporary SQLite DB path
- Sample Config instance
- Subprocess invocation helpers for CLI and hook tests
"""

import sqlite3
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    """Temporary archive root with a single project subdir containing sample .jsonl files.

    Structure:
        tmp_path/
            projects/
                test-project/
                    session-short.jsonl     (copied from fixtures)
                    session-malformed.jsonl (copied from fixtures)

    TODO(implementer): copy fixtures into tmp_path, return archive root.
    """
    raise NotImplementedError("See docs/PLAN.md section 11 for test plan.")


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Temporary SQLite DB path. File does not exist yet; storage.open_db() creates it."""
    return tmp_path / "test_index.db"


@pytest.fixture
def db_conn(temp_db: Path) -> sqlite3.Connection:
    """Opened, schema-initialized DB connection for tests that need one."""
    # TODO(implementer): return storage.open_db(temp_db)
    raise NotImplementedError
