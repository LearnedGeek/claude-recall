"""Tests for claude_recall.projects (v0.2.1 auto project scoping)."""

from pathlib import Path

from claude_recall import projects, storage


def test_slug_from_windows_path_lowercases_drive_letter():
    assert (
        projects.slug_from_path("E:\\Documents\\Work\\dev\\repos\\claude-recall")
        == "e--Documents-Work-dev-repos-claude-recall"
    )


def test_slug_from_unix_path_replaces_separators():
    # Leading '/' becomes a leading '-', matching the observed Claude Code convention.
    assert (
        projects.slug_from_path("/Users/markm/dev/claude-recall")
        == "-Users-markm-dev-claude-recall"
    )


def test_slug_preserves_existing_case_for_non_drive_segments():
    """Path segments keep their case; only the drive letter is lowercased."""
    slug = projects.slug_from_path("E:\\Documents\\Work\\CamelCaseDir")
    assert slug == "e--Documents-Work-CamelCaseDir"


def test_slug_handles_forward_slashes_on_windows():
    assert (
        projects.slug_from_path("E:/Documents/Work/dev")
        == "e--Documents-Work-dev"
    )


def test_resolve_project_slug_returns_stored_slug_case_insensitive(tmp_path):
    """If the DB has 'E--Old' and cwd maps to 'e--old', return 'E--Old'."""
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions(session_id, project_slug, file_path, "
            "file_mtime, turn_count, indexed_at) VALUES (?,?,?,?,?,?)",
            ("s1", "E--Documents-Work-Legacy", "/tmp/x.jsonl", 1.0, 0, "now"),
        )
        conn.commit()

        resolved = projects.resolve_project_slug(conn, "E:\\Documents\\Work\\Legacy")
        # The canonical slug is lowercase; the stored slug is uppercase. The
        # resolver returns the stored form so the SQL filter hits.
        assert resolved == "E--Documents-Work-Legacy"
    finally:
        conn.close()


def test_resolve_project_slug_falls_back_to_canonical_when_missing(tmp_path):
    """When no session has a matching slug, return the canonical derivation anyway."""
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        resolved = projects.resolve_project_slug(conn, "E:\\some\\path")
        assert resolved == "e--some-path"
    finally:
        conn.close()


def test_resolve_project_slug_uses_cwd_by_default(tmp_path, monkeypatch):
    """When cwd is None, the helper uses Path.cwd()."""
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    monkeypatch.chdir(tmp_path)
    try:
        resolved = projects.resolve_project_slug(conn)
        expected = projects.slug_from_path(Path(tmp_path).absolute())
        assert resolved == expected
    finally:
        conn.close()
