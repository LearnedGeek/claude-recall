"""Detect orphan/missing claude-recall slug archives (v0.11.0).

This module catches the failure mode that produced the strictlyelvisshow
data-loss incident on 2026-06-11:

1. A bulk-migration script (in that case, `repo_migrate.ps1` from May 26)
   moves project directories but misses some of the corresponding
   ``claude-recall`` slug archives.
2. The old-style slug (e.g. ``e--Documents-Work-strictlyelvisshow``)
   keeps the sessions but is no longer reached by Claude Code, because
   sessions opened from the new project path land under a new slug
   (e.g. ``e--dev-work-strictlyelvisshow``) which has no inherited
   history.
3. The next person to run a disk cleanup deletes the May-26 migration
   backup (the only remaining copy) without realizing it. The sessions
   are then unrecoverable from anywhere but cloud backup.

`claude-recall orphan-slugs` surfaces three categories of finding:

- **Orphan slug** — a directory under ``~/.claude/projects/`` whose
  inferred source path no longer exists on disk and which is not
  claimed by any expected-paths input. The archive is intact but
  unreachable by any active Claude Code session.
- **Pre-migration slug with a live successor** — an orphan slug whose
  path uses an old-style prefix (e.g. ``e--Documents-…``) that maps
  to a live new-style slug at a current location. These need
  ``claude-recall migrate --from <old> --to <new>`` to merge the
  sessions into the active slug. This is the specific shape that
  produced the SES incident.
- **Missing slug** — a project path supplied as expected that has no
  corresponding archive directory. Informational only — Claude Code
  creates the slug directory on first session in that project.

Inputs:

- ``--projects-json <path>`` to point at a Project Manager
  ``projects.json`` (or any JSON file with a list of
  ``{ rootPath: "..." }`` objects). On Windows, this is auto-detected
  at ``%APPDATA%/Code/User/globalStorage/alefragnani.project-manager/projects.json``.
- ``--path <p>`` (repeatable) to add expected project paths from the
  command line.
- ``--projects-roots <p>`` (repeatable) to declare directory roots
  whose immediate children are projects (e.g. ``e:/dev/work``,
  ``e:/dev/personal``). Useful when you don't use Project Manager.

If no inputs are given, the command runs with **filesystem-only mode**:
every slug archive is reverse-resolved to a path; if the path doesn't
exist the slug is flagged as an orphan. This catches the common case
without requiring any setup.

Exit codes:

- 0 — clean (no orphans, no recommended migrations)
- 1 — warnings only (orphan slugs that don't need a migration)
- 2 — actionable findings (pre-migration slug with a live successor)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .projects import slug_from_path

# Source-path prefixes that mark "pre-migration" slugs in this codebase.
# Customisable via OrphanSlugConfig.old_style_path_markers if needed
# (Mark's project tree uses ``e:\Documents\…`` for pre-May-26 paths).
DEFAULT_OLD_STYLE_MARKERS = ("Documents",)

# Auto-detected location of VSCode Project Manager's project list.
DEFAULT_PROJECTS_JSON_RELATIVE = (
    "Code/User/globalStorage/alefragnani.project-manager/projects.json"
)


@dataclass
class OrphanFinding:
    """One finding from an orphan-slug scan."""

    kind: str            # "orphan_slug" | "pre_migration_slug" | "missing_slug"
    severity: str        # "OK" | "WARN" | "ERROR"
    slug: str            # the filesystem slug (or expected slug for missing)
    inferred_path: Path | None = None
    suggested_target_slug: str | None = None
    suggested_action: str = ""
    detail: str = ""


@dataclass
class OrphanReport:
    """Outcome of one orphan-slug scan."""

    archive_root: Path
    findings: list[OrphanFinding] = field(default_factory=list)
    slugs_scanned: int = 0
    expected_paths_count: int = 0

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "ERROR" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == "WARN" for f in self.findings)


def _read_projects_json(path: Path) -> list[Path]:
    """Extract project root paths from a Project Manager-style projects.json.

    Returns a deduplicated list of Paths. Tolerates missing files and
    malformed JSON by returning an empty list.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    paths: list[Path] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        root = entry.get("rootPath")
        if isinstance(root, str) and root.strip():
            # Skip placeholder entries ("Root Path" is the default before
            # the user edits projects.json the first time).
            if root.strip().lower() == "root path":
                continue
            paths.append(Path(root))
    # Dedupe while preserving order.
    seen: set[str] = set()
    deduped: list[Path] = []
    for p in paths:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def _enumerate_project_root_children(root: Path) -> Iterator[Path]:
    """Yield immediate child directories of *root* that look like projects.

    Filters out hidden directories (starting with ``.``) and known
    non-project conventions (``__pycache__``, ``node_modules``). Does
    not recurse — the caller passes each tier explicitly (e.g.
    ``e:/dev/work``, ``e:/dev/personal``).
    """
    if not root.is_dir():
        return
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if child.name in {"__pycache__", "node_modules", "obj", "bin"}:
            continue
        yield child


def _gather_expected_paths(
    *, projects_json: Path | None,
    explicit_paths: Iterable[Path],
    projects_roots: Iterable[Path],
) -> list[Path]:
    """Collect expected project paths from all input sources."""
    paths: list[Path] = []
    if projects_json is not None:
        paths.extend(_read_projects_json(projects_json))
    paths.extend(explicit_paths)
    for root in projects_roots:
        paths.extend(_enumerate_project_root_children(root))
    # Dedupe.
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _reverse_slug_to_path(slug: str) -> Path | None:
    """Best-effort reverse of slug_from_path.

    The slug format replaces ``:``, ``\\`` and ``/`` all with ``-``, so
    the reverse is ambiguous (we can't tell which ``-`` was originally
    a separator versus literal). We assume the most-common case: every
    ``-`` after the drive letter was a separator. On Windows the result
    looks like ``e:/Documents/Work/strictlyelvisshow``.

    Returns None if the slug is too short to even parse a drive letter.
    """
    if len(slug) < 4:
        return None
    # Expected shape: ``<drive-letter>--<segments-joined-by-dashes>``
    # Lower-case drive letter then two literal dashes (one for the
    # original ``:``, one for the trailing path separator).
    if slug[1:3] != "--":
        return None
    drive = slug[0]
    if not drive.isalpha():
        return None
    rest = slug[3:]
    if not rest:
        return None
    # Translate dashes back to forward slashes; Path will normalise.
    path_str = f"{drive}:/{rest.replace('-', '/')}"
    return Path(path_str)


def _is_pre_migration_slug(slug: str, markers: Iterable[str]) -> bool:
    """True if *slug* looks like a pre-migration slug worth flagging.

    Matches when the slug contains any of the configured markers in a
    case-sensitive segment position. The default marker is
    ``"Documents"`` (Mark's pre-May-26 path convention).
    """
    parts = slug.split("-")
    return any(marker in parts for marker in markers)


def _find_live_successor_slug(
    pre_slug: str, all_slugs: Iterable[str], markers: Iterable[str],
) -> str | None:
    """Heuristic: find a live slug that looks like the post-migration form.

    Strategy: strip the marker segments from *pre_slug* and look for a
    live slug whose tail (last few segments) matches. E.g.
    ``e--Documents-Work-strictlyelvisshow`` stripped of ``Documents``
    and ``Work`` becomes ``e----strictlyelvisshow``; we look for any
    live slug ending in ``strictlyelvisshow``.

    Returns the first matching live slug, or None if no obvious
    successor is found.
    """
    parts = pre_slug.split("-")
    tail_parts = [p for p in parts if p and p not in markers]
    if not tail_parts:
        return None
    # The most specific part is the last one — that's typically the
    # project's own name and the best matching anchor.
    project_name = tail_parts[-1]
    for other in all_slugs:
        if other == pre_slug:
            continue
        if other.endswith(f"-{project_name}"):
            return other
    return None


def _list_filesystem_slugs(archive_root: Path) -> list[str]:
    """Return the names of every immediate child directory of *archive_root*."""
    if not archive_root.is_dir():
        return []
    return sorted(
        p.name for p in archive_root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def run_orphan_check(
    archive_root: Path,
    *,
    projects_json: Path | None = None,
    explicit_paths: Iterable[Path] = (),
    projects_roots: Iterable[Path] = (),
    old_style_markers: Iterable[str] = DEFAULT_OLD_STYLE_MARKERS,
) -> OrphanReport:
    """Scan the archive root for orphan slugs and missing slugs.

    See module docstring for the categorization rules and exit-code
    semantics.
    """
    report = OrphanReport(archive_root=archive_root)
    filesystem_slugs = _list_filesystem_slugs(archive_root)
    report.slugs_scanned = len(filesystem_slugs)
    expected_paths = _gather_expected_paths(
        projects_json=projects_json,
        explicit_paths=explicit_paths,
        projects_roots=projects_roots,
    )
    report.expected_paths_count = len(expected_paths)
    expected_slug_to_path: dict[str, Path] = {
        slug_from_path(p.resolve() if p.exists() else p): p
        for p in expected_paths
    }
    expected_slugs = set(expected_slug_to_path.keys())

    # Pass 1: orphan slugs (in filesystem, not in expected, source path
    # doesn't exist).
    # For successor search we consider BOTH filesystem slugs and expected
    # slugs — a valid successor may be an expected slug that hasn't
    # accumulated any sessions yet (no archive dir, but Claude Code
    # would create it on first use).
    successor_candidates = list(filesystem_slugs) + list(expected_slugs)
    for slug in filesystem_slugs:
        if slug in expected_slugs:
            continue
        inferred = _reverse_slug_to_path(slug)
        if inferred is not None and inferred.exists():
            # Live slug — not in projects.json but its source path
            # still exists, so this is just a project the user hasn't
            # bothered to add to the Project Manager list. Skip.
            continue
        # It's orphan-shaped. Check whether it's pre-migration with a
        # live successor; that's the actionable case.
        successor = None
        if _is_pre_migration_slug(slug, old_style_markers):
            successor = _find_live_successor_slug(
                slug, successor_candidates, old_style_markers,
            )
        if successor is not None:
            report.findings.append(OrphanFinding(
                kind="pre_migration_slug",
                severity="ERROR",
                slug=slug,
                inferred_path=inferred,
                suggested_target_slug=successor,
                suggested_action=(
                    f"claude-recall migrate --from {slug} --to {successor}"
                ),
                detail=(
                    "Source path is gone; a live slug appears to be the "
                    "post-migration successor. Migrate to merge history."
                ),
            ))
        else:
            report.findings.append(OrphanFinding(
                kind="orphan_slug",
                severity="WARN",
                slug=slug,
                inferred_path=inferred,
                suggested_target_slug=None,
                suggested_action=(
                    "Project path is gone. If you want to keep the history, "
                    f"leave the slug in place; if not, `rm -r {archive_root}/{slug}` "
                    "or run `claude-recall migrate --from {slug} --to <new-slug>` "
                    "if you know the successor."
                ),
                detail=(
                    "No active project resolves to this slug, and no "
                    "obvious successor exists in the archive."
                ),
            ))

    # Pass 2: missing slugs (expected but no archive directory yet).
    fs_set = set(filesystem_slugs)
    for slug, path in expected_slug_to_path.items():
        if slug in fs_set:
            continue
        report.findings.append(OrphanFinding(
            kind="missing_slug",
            severity="OK",
            slug=slug,
            inferred_path=path,
            suggested_action=(
                "No Claude Code session has run in this project yet; "
                "the slug directory will be created on first session."
            ),
        ))

    return report


def format_report(report: OrphanReport) -> str:
    """Human-readable summary of an orphan-slug scan."""
    lines: list[str] = []
    lines.append(f"archive root: {report.archive_root}")
    lines.append(
        f"slugs scanned: {report.slugs_scanned}, "
        f"expected paths: {report.expected_paths_count}"
    )

    errors = [f for f in report.findings if f.severity == "ERROR"]
    warnings = [f for f in report.findings if f.severity == "WARN"]
    infos = [f for f in report.findings if f.severity == "OK"]

    if errors:
        lines.append("")
        lines.append(
            f"[ERROR] {len(errors)} pre-migration slug(s) with a live "
            f"successor — action recommended:"
        )
        for f in errors:
            lines.append(f"  - {f.slug}")
            lines.append(f"      successor: {f.suggested_target_slug}")
            lines.append(f"      → {f.suggested_action}")

    if warnings:
        lines.append("")
        lines.append(
            f"[WARN]  {len(warnings)} orphan slug(s) — project path no "
            f"longer exists:"
        )
        for f in warnings:
            lines.append(f"  - {f.slug}")
            if f.inferred_path is not None:
                lines.append(f"      inferred path: {f.inferred_path} (missing)")
            lines.append(f"      → {f.suggested_action}")

    if infos:
        lines.append("")
        lines.append(
            f"[OK]    {len(infos)} expected project(s) with no archive "
            f"yet — informational:"
        )
        for f in infos[:10]:
            lines.append(f"  - {f.slug}  ({f.inferred_path})")
        if len(infos) > 10:
            lines.append(f"  ... and {len(infos) - 10} more")

    if not report.findings:
        lines.append("")
        lines.append("all slugs accounted for — no orphans, no missing.")

    return "\n".join(lines)


def default_projects_json_path() -> Path | None:
    """Return the platform-conventional Project Manager projects.json path.

    On Windows this is ``%APPDATA%/Code/User/globalStorage/.../projects.json``.
    Returns None if the env-var lookup fails or the path doesn't exist.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    candidate = Path(appdata) / DEFAULT_PROJECTS_JSON_RELATIVE
    if candidate.exists():
        return candidate
    return None
