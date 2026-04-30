"""Project-archive relocation helper (v0.8.2, issue #25).

When a code project moves on disk (e.g., ``E:\\Documents\\Work\\dev\\repos\\foo``
→ ``E:\\dev\\repos\\foo``), Claude Code starts writing future sessions
under a new slug derived from the new path. The old archive directory
stays where it was; the link between active project location and prior
archive history is silently severed. claude-recall keeps indexing the
old slug, but the SessionStart hook from the new location auto-scopes to
the new slug — prior recall doesn't surface unless the user knows to
search ``--project <old-slug>``.

``run_migrate`` fixes this in one transactional step: move the archive
directory under the new slug, update ``sessions.project_slug`` and
``sessions.file_path`` for every affected row. **Vectors are preserved**
— msg_id PKs don't change, so the FK chain to ``message_vectors`` holds
without re-embedding.

Same-machine relocation only. Cross-machine migration (move JSONLs to a
different host) is the ``claude-recall index`` path on the destination —
no existing DB to update.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path


class MigrateError(RuntimeError):
    """Raised for terminal failures the CLI should catch and exit on."""


@dataclass
class MigrateReport:
    from_slug: str
    to_slug: str
    sessions_migrated: int = 0
    messages_migrated: int = 0
    vectors_preserved: int = 0
    archive_dir_moved: bool = False
    dry_run: bool = False


def run_migrate(
    conn: sqlite3.Connection,
    archive_root: Path,
    *,
    from_slug: str,
    to_slug: str,
    force: bool = False,
    dry_run: bool = False,
) -> MigrateReport:
    """Migrate a project archive from one slug to another (same machine).

    Atomically moves ``<archive_root>/<from_slug>/`` to
    ``<archive_root>/<to_slug>/`` and updates the DB rows so all
    existing ``messages``, ``message_vectors``, and FTS5 entries
    transfer untouched (msg_id PKs stay stable; only ``sessions``
    columns change).

    On failure during the DB update, the archive-directory move is
    reverted so the user is not left in a half-state.

    Args:
        conn: open DB connection (caller owns lifecycle)
        archive_root: parent directory containing the per-project slug
            subdirectories (typically ``~/.claude/projects``)
        from_slug: existing project slug to migrate from
        to_slug: target project slug
        force: if True, allow overwriting an existing target slug
            directory (rare; prefer to investigate the conflict)
        dry_run: if True, return what would happen without making any
            disk or DB changes

    Returns:
        MigrateReport summarizing affected rows. ``archive_dir_moved``
        is True only if the disk move succeeded; ``dry_run`` reflects
        the parameter value for downstream callers/tests.
    """
    archive_root = Path(archive_root)
    old_dir = archive_root / from_slug
    new_dir = archive_root / to_slug

    if from_slug == to_slug:
        raise MigrateError(
            f"--from and --to are identical: {from_slug!r}. "
            f"Migration would be a no-op."
        )

    if not old_dir.is_dir():
        raise MigrateError(
            f"source archive directory does not exist: {old_dir}. "
            f"Verify the --from slug matches an existing directory under "
            f"{archive_root}."
        )

    if new_dir.exists() and not force:
        raise MigrateError(
            f"target archive directory already exists: {new_dir}. "
            f"This usually means Claude Code already started writing "
            f"sessions to the new location. Investigate the conflict "
            f"before passing --force (which deletes the target dir)."
        )

    # Snapshot what will be affected. Vector count via JOIN so the
    # report tells the user "vectors are preserved" with a real number
    # instead of an abstract claim.
    rows = conn.execute(
        "SELECT s.session_id, s.file_path, "
        "  (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id) AS msg_count, "
        "  (SELECT COUNT(*) FROM message_vectors v "
        "   JOIN messages m ON m.msg_id = v.msg_id "
        "   WHERE m.session_id = s.session_id) AS vec_count "
        "FROM sessions s WHERE project_slug = ?",
        (from_slug,),
    ).fetchall()

    report = MigrateReport(
        from_slug=from_slug,
        to_slug=to_slug,
        sessions_migrated=len(rows),
        messages_migrated=sum(r["msg_count"] for r in rows),
        vectors_preserved=sum(r["vec_count"] for r in rows),
        dry_run=dry_run,
    )

    if dry_run:
        return report

    # Move the archive directory first. If this fails, no DB damage.
    if new_dir.exists() and force:
        shutil.rmtree(new_dir)
    shutil.move(str(old_dir), str(new_dir))
    report.archive_dir_moved = True

    # Update DB rows in a single transaction. Use Python-side string
    # replacement on file_path rather than SQL REPLACE() so we avoid
    # any partial-match issues with slugs that share a prefix.
    old_path_prefix = str(old_dir)
    new_path_prefix = str(new_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            old_file_path = row["file_path"]
            # replace() with count=1 to avoid pathological double-substitution
            # if the slug somehow appears twice in the path (it shouldn't,
            # but defense in depth).
            new_file_path = old_file_path.replace(
                old_path_prefix, new_path_prefix, 1
            )
            conn.execute(
                "UPDATE sessions "
                "SET project_slug = ?, file_path = ? "
                "WHERE session_id = ?",
                (to_slug, new_file_path, row["session_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        # Best-effort rollback of the disk move so the user isn't left
        # in a half-state where the dir is at the new path but the DB
        # still references the old.
        try:
            shutil.move(str(new_dir), str(old_dir))
            report.archive_dir_moved = False
        except OSError:
            # If even the rollback fails, surface the original error;
            # the user has a recoverable state on disk (dir at new path,
            # DB still pointing at old path) which is fixable manually.
            pass
        raise

    return report


def format_report(report: MigrateReport) -> str:
    """Human-readable summary for the CLI."""
    lines: list[str] = []
    if report.dry_run:
        lines.append(
            f"[dry-run] Would migrate {report.sessions_migrated} sessions, "
            f"{report.messages_migrated:,} messages, "
            f"{report.vectors_preserved:,} vectors"
        )
        lines.append(
            f"[dry-run] Would rename archive directory: "
            f"{report.from_slug!r} → {report.to_slug!r}"
        )
        lines.append("[dry-run] No changes made.")
    else:
        lines.append(
            f"Migrated {report.sessions_migrated} sessions, "
            f"{report.messages_migrated:,} messages, "
            f"{report.vectors_preserved:,} vectors preserved."
        )
        lines.append(
            f"Archive directory: {report.from_slug!r} → {report.to_slug!r}"
        )
        lines.append("Run `claude-recall status` to verify.")
    return "\n".join(lines)
