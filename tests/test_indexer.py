"""Tests for claude_recall.indexer.

See docs/PLAN.md section 11 for the full test plan.
"""

import pytest


@pytest.mark.skip(reason="indexer.py not yet implemented")
def test_index_fresh_archive(archive_dir, db_conn):
    """First-time index of a clean archive inserts expected session + message counts."""
    pass


@pytest.mark.skip(reason="indexer.py not yet implemented")
def test_incremental_skips_unchanged_files(archive_dir, db_conn):
    """Re-running index with no file changes reports 0 new / 0 updated."""
    pass


@pytest.mark.skip(reason="indexer.py not yet implemented")
def test_changed_file_triggers_reindex(archive_dir, db_conn):
    """Touching a file's mtime causes re-index of just that session."""
    pass


@pytest.mark.skip(reason="indexer.py not yet implemented")
def test_malformed_line_does_not_crash(archive_dir, db_conn):
    """Malformed JSONL lines are skipped with a count, index continues."""
    pass


@pytest.mark.skip(reason="indexer.py not yet implemented")
def test_rebuild_replaces_cleanly(archive_dir, db_conn):
    """--rebuild drops and re-inserts without orphaned rows."""
    pass


@pytest.mark.skip(reason="indexer.py not yet implemented")
def test_tool_blocks_skipped_by_default(archive_dir, db_conn):
    """tool_use / tool_result blocks are not indexed unless opt-in."""
    pass
