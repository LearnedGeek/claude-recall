"""Tests for the orphan-slugs subcommand (v0.11.0)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_recall import orphan_slugs


# --- helpers -----------------------------------------------------------------


def _slug_dir(archive_root: Path, slug: str) -> Path:
    """Create an empty slug directory under *archive_root* and return it."""
    d = archive_root / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_projects_json(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


# --- reverse-slug parser -----------------------------------------------------


def test_reverse_slug_handles_typical_windows_shape():
    p = orphan_slugs._reverse_slug_to_path("e--dev-work-claude-recall")
    assert p == Path("e:/dev/work/claude/recall")


def test_reverse_slug_returns_none_on_too_short():
    assert orphan_slugs._reverse_slug_to_path("e-") is None
    assert orphan_slugs._reverse_slug_to_path("") is None


def test_reverse_slug_returns_none_when_drive_marker_missing():
    # Real slugs always have ``--`` after the drive letter; without
    # that the reverse is ambiguous.
    assert orphan_slugs._reverse_slug_to_path("nodriveprefix") is None


# --- projects.json reading ---------------------------------------------------


def test_read_projects_json_extracts_paths(tmp_path):
    pj = _write_projects_json(tmp_path / "projects.json", [
        {"name": "A", "rootPath": "E:\\dev\\work\\A", "tags": []},
        {"name": "B", "rootPath": "E:\\dev\\personal\\B", "tags": []},
    ])
    paths = orphan_slugs._read_projects_json(pj)
    assert len(paths) == 2
    assert Path("E:\\dev\\work\\A") in paths


def test_read_projects_json_dedupes_and_drops_placeholders(tmp_path):
    pj = _write_projects_json(tmp_path / "projects.json", [
        {"name": "Project Name", "rootPath": "Root Path"},      # placeholder
        {"name": "A", "rootPath": "E:\\dev\\work\\A"},
        {"name": "Dup", "rootPath": "e:\\dev\\work\\a"},        # case-dup
    ])
    paths = orphan_slugs._read_projects_json(pj)
    assert len(paths) == 1


def test_read_projects_json_tolerates_malformed_input(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("this is not json", encoding="utf-8")
    assert orphan_slugs._read_projects_json(bad) == []


def test_read_projects_json_returns_empty_for_missing_file(tmp_path):
    assert orphan_slugs._read_projects_json(tmp_path / "nope.json") == []


# --- pre-migration detection -------------------------------------------------


def test_is_pre_migration_slug_matches_default_marker():
    assert orphan_slugs._is_pre_migration_slug(
        "e--Documents-Work-strictlyelvisshow", ("Documents",)
    )


def test_is_pre_migration_slug_negative_when_marker_absent():
    assert not orphan_slugs._is_pre_migration_slug(
        "e--dev-work-strictlyelvisshow", ("Documents",)
    )


def test_find_live_successor_matches_by_project_name_tail():
    successor = orphan_slugs._find_live_successor_slug(
        "e--Documents-Work-strictlyelvisshow",
        all_slugs=[
            "e--Documents-Work-strictlyelvisshow",
            "e--dev-work-strictlyelvisshow",
            "e--dev-work-CrewTrack",
        ],
        markers=("Documents",),
    )
    assert successor == "e--dev-work-strictlyelvisshow"


def test_find_live_successor_returns_none_when_no_match():
    successor = orphan_slugs._find_live_successor_slug(
        "e--Documents-old-project",
        all_slugs=["e--dev-work-CrewTrack", "e--dev-personal-DND"],
        markers=("Documents",),
    )
    assert successor is None


# --- end-to-end orphan check -------------------------------------------------


def test_run_orphan_check_flags_pre_migration_with_successor(tmp_path):
    archive = tmp_path / "projects"
    archive.mkdir()

    # Old-style slug whose source path is gone.
    _slug_dir(archive, "e--Documents-Work-strictlyelvisshow")
    # Live successor slug (source path will be created so it doesn't
    # also flag as orphan).
    _slug_dir(archive, "e--dev-work-strictlyelvisshow")

    project = tmp_path / "dev" / "work" / "strictlyelvisshow"
    project.mkdir(parents=True)

    report = orphan_slugs.run_orphan_check(
        archive,
        explicit_paths=[project],
    )

    errors = [f for f in report.findings if f.severity == "ERROR"]
    assert len(errors) == 1
    f = errors[0]
    assert f.kind == "pre_migration_slug"
    assert f.slug == "e--Documents-Work-strictlyelvisshow"
    assert f.suggested_target_slug == "e--dev-work-strictlyelvisshow"
    assert "claude-recall migrate" in f.suggested_action


def test_run_orphan_check_finds_successor_from_expected_paths_only(tmp_path):
    """The Manuscript-FULL case: pre-migration slug has sessions, but the
    successor slug has no archive yet because Claude Code hasn't run in
    the new path. The expected path (projects.json) drives the match."""
    archive = tmp_path / "projects"
    archive.mkdir()
    # Old slug has the sessions on disk.
    _slug_dir(archive, "e--Documents-Hobbies-old-Manuscript-FULL")
    # New project path exists but no archive dir yet.
    project = tmp_path / "dev" / "personal" / "Manuscript-FULL"
    project.mkdir(parents=True)

    report = orphan_slugs.run_orphan_check(
        archive, explicit_paths=[project],
    )

    errors = [f for f in report.findings if f.severity == "ERROR"]
    assert len(errors) == 1
    assert errors[0].kind == "pre_migration_slug"
    assert errors[0].suggested_target_slug == orphan_slugs.slug_from_path(project)


def test_run_orphan_check_flags_orphan_with_no_successor(tmp_path):
    archive = tmp_path / "projects"
    archive.mkdir()

    # A slug whose source path doesn't exist and no successor is present.
    _slug_dir(archive, "e--dev-personal-deleted-game-project")

    report = orphan_slugs.run_orphan_check(archive)

    warnings = [f for f in report.findings if f.severity == "WARN"]
    assert len(warnings) == 1
    assert warnings[0].kind == "orphan_slug"


def test_run_orphan_check_does_not_flag_live_slugs(tmp_path):
    archive = tmp_path / "projects"
    archive.mkdir()
    project = tmp_path / "dev" / "work" / "current"
    project.mkdir(parents=True)
    # Slug derived from the live path.
    _slug_dir(archive, orphan_slugs.slug_from_path(project))

    report = orphan_slugs.run_orphan_check(
        archive,
        explicit_paths=[project],
    )

    assert not report.has_errors
    assert not report.has_warnings


def test_run_orphan_check_flags_missing_slug_as_info(tmp_path):
    archive = tmp_path / "projects"
    archive.mkdir()
    project = tmp_path / "dev" / "work" / "noTSession"
    project.mkdir(parents=True)
    # Note: no slug directory yet.

    report = orphan_slugs.run_orphan_check(
        archive,
        explicit_paths=[project],
    )

    infos = [f for f in report.findings if f.severity == "OK"]
    assert len(infos) == 1
    assert infos[0].kind == "missing_slug"


def test_run_orphan_check_projects_json_drives_expectations(tmp_path):
    archive = tmp_path / "projects"
    archive.mkdir()
    project_a = tmp_path / "dev" / "work" / "ProjA"
    project_a.mkdir(parents=True)
    pj = _write_projects_json(tmp_path / "projects.json", [
        {"name": "ProjA", "rootPath": str(project_a)},
    ])

    # No archive dir for ProjA — should appear as missing_slug.
    report = orphan_slugs.run_orphan_check(
        archive, projects_json=pj,
    )
    missing = [f for f in report.findings if f.kind == "missing_slug"]
    assert any(orphan_slugs.slug_from_path(project_a) == f.slug for f in missing)


def test_run_orphan_check_projects_root_enumerates_children(tmp_path):
    archive = tmp_path / "projects"
    archive.mkdir()
    work_root = tmp_path / "dev" / "work"
    (work_root / "Alpha").mkdir(parents=True)
    (work_root / "Beta").mkdir()
    (work_root / "__pycache__").mkdir()   # should be filtered out
    (work_root / ".hidden").mkdir()       # should be filtered out

    report = orphan_slugs.run_orphan_check(
        archive, projects_roots=[work_root],
    )
    assert report.expected_paths_count == 2


def test_run_orphan_check_ignores_archive_root_missing():
    report = orphan_slugs.run_orphan_check(Path("does-not-exist"))
    assert report.slugs_scanned == 0
    assert report.findings == []


def test_format_report_renders_error_section(tmp_path):
    archive = tmp_path / "projects"
    archive.mkdir()
    _slug_dir(archive, "e--Documents-Work-strictlyelvisshow")
    _slug_dir(archive, "e--dev-work-strictlyelvisshow")
    project = tmp_path / "dev" / "work" / "strictlyelvisshow"
    project.mkdir(parents=True)
    report = orphan_slugs.run_orphan_check(
        archive, explicit_paths=[project],
    )
    out = orphan_slugs.format_report(report)
    assert "[ERROR]" in out
    assert "claude-recall migrate" in out


def test_format_report_renders_clean_message(tmp_path):
    archive = tmp_path / "projects"
    archive.mkdir()
    out = orphan_slugs.format_report(
        orphan_slugs.run_orphan_check(archive),
    )
    assert "no orphans" in out.lower()
