"""Tests for claude_recall.migrate (v0.8.2, issue #25)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from claude_recall import embeddings, indexer, migrate, storage


def _seed_archive(archive_root: Path, slug: str, *, count: int = 3) -> Path:
    """Create an archive directory with `count` minimal JSONL session files."""
    project_dir = archive_root / slug
    project_dir.mkdir(parents=True)
    for i in range(count):
        jsonl = project_dir / f"session-{i}.jsonl"
        lines = [
            '{"type":"user","message":{"role":"user","content":'
            f'"hello {slug} variant {i}"}},'
            f'"timestamp":"2026-04-30T12:0{i}:00Z"}}'.replace(
                '}},"timestamp"', '},"timestamp"'
            ),
        ]
        # Cleaner JSON construction.
        lines = []
        for j in range(2):
            lines.append(
                '{"type":"user","message":{"role":"user","content":"'
                f"hello {slug} session {i} turn {j}"
                '"},"timestamp":"' + f"2026-04-30T12:0{i}:0{j}Z" + '"}'
            )
        jsonl.write_text("\n".join(lines), encoding="utf-8")
    return project_dir


def test_migrate_moves_archive_dir_and_updates_db_rows(tmp_path):
    """Happy path: archive dir is moved, DB rows updated, no data loss."""
    archive_root = tmp_path / "projects"
    _seed_archive(archive_root, "old-slug", count=3)

    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        indexer.run_index(conn, archive_root)
        # Pre-state: 3 sessions under old-slug.
        pre_old = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project_slug = 'old-slug'"
        ).fetchone()[0]
        assert pre_old == 3

        report = migrate.run_migrate(
            conn, archive_root, from_slug="old-slug", to_slug="new-slug",
        )

        assert report.sessions_migrated == 3
        assert report.archive_dir_moved is True
        assert report.dry_run is False

        # Post-state: directory moved.
        assert not (archive_root / "old-slug").exists()
        assert (archive_root / "new-slug").is_dir()

        # Post-state: rows updated.
        new_count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project_slug = 'new-slug'"
        ).fetchone()[0]
        old_count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project_slug = 'old-slug'"
        ).fetchone()[0]
        assert new_count == 3
        assert old_count == 0

        # Post-state: file_path values point at the new archive location.
        rows = conn.execute(
            "SELECT file_path FROM sessions WHERE project_slug = 'new-slug'"
        ).fetchall()
        for r in rows:
            assert "new-slug" in r["file_path"]
            assert "old-slug" not in r["file_path"]
    finally:
        conn.close()


def test_migrate_preserves_message_vectors(tmp_path):
    """Vectors must survive the migrate. msg_id PKs don't change, so the
    FK chain to message_vectors is intact — no re-embed required."""
    archive_root = tmp_path / "projects"
    _seed_archive(archive_root, "embed-old", count=2)

    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        indexer.run_index(conn, archive_root)

        # Manually seed vectors for the messages we just indexed.
        msg_ids = [
            r["msg_id"]
            for r in conn.execute(
                "SELECT m.msg_id FROM messages m "
                "JOIN sessions s ON s.session_id = m.session_id "
                "WHERE s.project_slug = 'embed-old' "
                "ORDER BY m.msg_id"
            ).fetchall()
        ]
        assert len(msg_ids) >= 2

        for mid in msg_ids:
            v = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
            conn.execute(
                "INSERT INTO message_vectors VALUES (?,?,?,?,?)",
                (mid, embeddings.pack_vector(v), "test-model", 4,
                 "2026-04-30T12:00:00Z"),
            )
        conn.commit()
        pre_vec_count = conn.execute(
            "SELECT COUNT(*) FROM message_vectors"
        ).fetchone()[0]
        assert pre_vec_count == len(msg_ids)

        report = migrate.run_migrate(
            conn, archive_root, from_slug="embed-old", to_slug="embed-new",
        )
        assert report.vectors_preserved == len(msg_ids)

        # Vectors still joinable by msg_id under the new slug.
        joined = conn.execute(
            "SELECT m.msg_id, v.vector FROM messages m "
            "JOIN message_vectors v USING (msg_id) "
            "JOIN sessions s ON s.session_id = m.session_id "
            "WHERE s.project_slug = 'embed-new'"
        ).fetchall()
        assert len(joined) == len(msg_ids)
        # Vector blobs unchanged.
        post_vec_count = conn.execute(
            "SELECT COUNT(*) FROM message_vectors"
        ).fetchone()[0]
        assert post_vec_count == pre_vec_count
    finally:
        conn.close()


def test_migrate_refuses_when_target_exists_without_force(tmp_path):
    """Destination slug already has a directory → MigrateError, exit 2 in CLI."""
    archive_root = tmp_path / "projects"
    _seed_archive(archive_root, "src-slug", count=1)
    _seed_archive(archive_root, "dest-slug", count=1)

    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        indexer.run_index(conn, archive_root)
        with pytest.raises(migrate.MigrateError) as excinfo:
            migrate.run_migrate(
                conn, archive_root,
                from_slug="src-slug", to_slug="dest-slug",
            )
        msg = str(excinfo.value).lower()
        assert "already exists" in msg
        assert "force" in msg
        # Both directories untouched.
        assert (archive_root / "src-slug").is_dir()
        assert (archive_root / "dest-slug").is_dir()
    finally:
        conn.close()


def test_migrate_force_overwrites_existing_target(tmp_path):
    """--force allows overwriting an existing target dir. Caller takes
    the risk of data loss; this is the rare 'I know what I'm doing' path."""
    archive_root = tmp_path / "projects"
    _seed_archive(archive_root, "src-slug", count=2)
    _seed_archive(archive_root, "dest-slug", count=1)

    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        indexer.run_index(conn, archive_root)
        report = migrate.run_migrate(
            conn, archive_root,
            from_slug="src-slug", to_slug="dest-slug",
            force=True,
        )
        # Source's 2 sessions ended up under dest-slug; the previous
        # dest-slug directory was overwritten.
        assert report.sessions_migrated == 2
        assert report.archive_dir_moved is True
    finally:
        conn.close()


def test_migrate_refuses_when_source_missing(tmp_path):
    archive_root = tmp_path / "projects"
    archive_root.mkdir()
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        with pytest.raises(migrate.MigrateError) as excinfo:
            migrate.run_migrate(
                conn, archive_root,
                from_slug="does-not-exist", to_slug="anything",
            )
        assert "does not exist" in str(excinfo.value).lower()
    finally:
        conn.close()


def test_migrate_refuses_same_slug(tmp_path):
    archive_root = tmp_path / "projects"
    _seed_archive(archive_root, "same-slug", count=1)
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        with pytest.raises(migrate.MigrateError) as excinfo:
            migrate.run_migrate(
                conn, archive_root,
                from_slug="same-slug", to_slug="same-slug",
            )
        assert "identical" in str(excinfo.value).lower()
    finally:
        conn.close()


def test_migrate_dry_run_makes_no_changes(tmp_path):
    archive_root = tmp_path / "projects"
    _seed_archive(archive_root, "preview-old", count=2)
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        indexer.run_index(conn, archive_root)

        # Snapshot state.
        pre_paths = sorted(p.name for p in archive_root.iterdir())
        pre_rows = conn.execute(
            "SELECT session_id, project_slug, file_path FROM sessions "
            "ORDER BY session_id"
        ).fetchall()

        report = migrate.run_migrate(
            conn, archive_root,
            from_slug="preview-old", to_slug="preview-new",
            dry_run=True,
        )

        # Report still summarizes what WOULD happen.
        assert report.sessions_migrated == 2
        assert report.dry_run is True
        assert report.archive_dir_moved is False

        # Disk state unchanged.
        post_paths = sorted(p.name for p in archive_root.iterdir())
        assert pre_paths == post_paths

        # DB state unchanged.
        post_rows = conn.execute(
            "SELECT session_id, project_slug, file_path FROM sessions "
            "ORDER BY session_id"
        ).fetchall()
        assert [tuple(r) for r in pre_rows] == [tuple(r) for r in post_rows]
    finally:
        conn.close()


def test_migrate_format_report_includes_counts(tmp_path):
    """The CLI output is the report's format_report() text — verify it
    contains the counts the user expects."""
    archive_root = tmp_path / "projects"
    _seed_archive(archive_root, "fmt-old", count=2)
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        indexer.run_index(conn, archive_root)
        report = migrate.run_migrate(
            conn, archive_root, from_slug="fmt-old", to_slug="fmt-new",
        )
        text = migrate.format_report(report)
        assert "2 sessions" in text
        assert "fmt-old" in text
        assert "fmt-new" in text
        assert "claude-recall status" in text
    finally:
        conn.close()


def test_migrate_dry_run_format_report_says_no_changes(tmp_path):
    archive_root = tmp_path / "projects"
    _seed_archive(archive_root, "dryfmt-old", count=1)
    db_path = tmp_path / "index.db"
    conn = storage.open_db(db_path)
    try:
        indexer.run_index(conn, archive_root)
        report = migrate.run_migrate(
            conn, archive_root,
            from_slug="dryfmt-old", to_slug="dryfmt-new",
            dry_run=True,
        )
        text = migrate.format_report(report)
        assert "[dry-run]" in text
        assert "No changes" in text
    finally:
        conn.close()
