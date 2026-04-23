"""Tests for claude_recall.search.

See docs/PLAN.md section 11 for the full test plan.
"""

import pytest


@pytest.mark.skip(reason="search.py not yet implemented")
def test_fts5_ranking_order(db_conn):
    """Known corpus produces known top-k results in expected order."""
    pass


@pytest.mark.skip(reason="search.py not yet implemented")
def test_threshold_filters_low_score(db_conn):
    """Results below BM25 threshold are dropped."""
    pass


@pytest.mark.skip(reason="search.py not yet implemented")
def test_snippet_marks_matches(db_conn):
    """Snippets include <mark>...</mark> around matched terms."""
    pass


@pytest.mark.skip(reason="search.py not yet implemented")
def test_days_filter(db_conn):
    """--days N excludes sessions older than the window."""
    pass


@pytest.mark.skip(reason="search.py not yet implemented")
def test_project_scope(db_conn):
    """--project limits results to one project slug."""
    pass


@pytest.mark.skip(reason="search.py not yet implemented")
def test_malformed_query_falls_back(db_conn):
    """Invalid FTS5 syntax falls back to quoted-phrase search without crashing."""
    pass
