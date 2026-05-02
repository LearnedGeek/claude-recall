"""Re-run the content-kind classifier across an existing index (v0.8.4).

The v0.6 indexer's hash-diff path means existing messages keep their
``content_kind`` value as long as their content hasn't changed. That's
the right behavior for ingest performance, but it means classifier
tuning (new HARNESS patterns, new PROCEDURAL openers, etc.) doesn't
retroactively apply to already-indexed rows.

``run_reclassify`` walks the messages table, re-runs
``content_kinds.classify`` against current content + role, and updates
the column where the verdict has changed. Reversible: re-running with
the same classifier code is a no-op; re-running after another
classifier tuning round picks up the new verdicts.

Scopes:
- Default: full corpus
- ``--project <slug>``: scope to one project (useful when iterating on
  a register-specific classifier extension and you only want to validate
  the change against one project's content before committing to a full
  reclassification)
- ``--dry-run``: preview the deltas without writing
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from . import content_kinds


@dataclass
class ReclassifyReport:
    total_examined: int = 0
    rows_changed: int = 0
    project_slug: Optional[str] = None
    dry_run: bool = False
    pre_distribution: dict[str, int] = field(default_factory=dict)
    post_distribution: dict[str, int] = field(default_factory=dict)


def run_reclassify(
    conn: sqlite3.Connection,
    *,
    project_slug: str | None = None,
    dry_run: bool = False,
) -> ReclassifyReport:
    """Re-run the classifier on every message in scope.

    Reads ``messages.content`` and ``messages.role``, re-classifies via
    ``content_kinds.classify``, and writes the new ``content_kind`` value
    when it differs from the stored one. The pre- and post-distributions
    are reported so the caller can see the deltas at a glance.
    """
    if project_slug:
        rows = conn.execute(
            "SELECT m.msg_id, m.content, m.role, m.content_kind "
            "FROM messages m "
            "JOIN sessions s ON s.session_id = m.session_id "
            "WHERE LOWER(s.project_slug) = LOWER(?)",
            (project_slug,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT msg_id, content, role, content_kind FROM messages"
        ).fetchall()

    pre_counter: Counter[str] = Counter()
    post_counter: Counter[str] = Counter()
    updates: list[tuple[str, int]] = []

    for row in rows:
        old_kind = row["content_kind"] or content_kinds.THOUGHT
        new_kind = content_kinds.classify(row["content"], row["role"])
        pre_counter[old_kind] += 1
        post_counter[new_kind] += 1
        if new_kind != row["content_kind"]:
            updates.append((new_kind, row["msg_id"]))

    if updates and not dry_run:
        conn.executemany(
            "UPDATE messages SET content_kind = ? WHERE msg_id = ?",
            updates,
        )
        conn.commit()

    return ReclassifyReport(
        total_examined=len(rows),
        rows_changed=len(updates),
        project_slug=project_slug,
        dry_run=dry_run,
        pre_distribution=dict(pre_counter),
        post_distribution=dict(post_counter),
    )


def format_report(report: ReclassifyReport) -> str:
    """Human-readable delta summary."""
    lines: list[str] = []
    scope = (
        f"project {report.project_slug!r}"
        if report.project_slug
        else "full corpus"
    )
    prefix = "[dry-run] Would reclassify" if report.dry_run else "Reclassified"
    lines.append(
        f"{prefix} {report.total_examined:,} messages ({scope})"
    )
    lines.append("")

    # Show per-kind distribution with deltas. Iterate over the union of
    # pre/post keys so kinds present in only one side still show up.
    kinds = sorted(
        set(report.pre_distribution) | set(report.post_distribution)
    )
    width = max((len(k) for k in kinds), default=10)
    for kind in kinds:
        pre = report.pre_distribution.get(kind, 0)
        post = report.post_distribution.get(kind, 0)
        delta = post - pre
        sign = "+" if delta > 0 else ""
        lines.append(
            f"  {kind:<{width}}  {pre:>7,} → {post:>7,}  ({sign}{delta:,})"
        )

    lines.append("")
    if report.dry_run:
        lines.append(
            f"[dry-run] {report.rows_changed:,} rows would change. "
            f"No writes performed."
        )
    else:
        lines.append(f"{report.rows_changed:,} rows updated.")
    return "\n".join(lines)
