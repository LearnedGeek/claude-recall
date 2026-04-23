"""Tests for claude_recall.storage.

See docs/PLAN.md section 11 for the full test plan.
"""

import pytest

# TODO(implementer): import storage, test schema init, version tracking, FTS5 check.


@pytest.mark.skip(reason="storage.py not yet implemented")
def test_open_db_creates_schema(temp_db):
    """open_db() creates the schema and returns a working connection."""
    pass


@pytest.mark.skip(reason="storage.py not yet implemented")
def test_schema_version_is_current(db_conn):
    """Schema version is recorded and matches SCHEMA_VERSION constant."""
    pass


@pytest.mark.skip(reason="storage.py not yet implemented")
def test_fts5_available(db_conn):
    """FTS5 is available in this SQLite build."""
    pass


@pytest.mark.skip(reason="storage.py not yet implemented")
def test_messages_fts_sync_on_insert(db_conn):
    """Inserting a message also populates messages_fts via trigger."""
    pass
