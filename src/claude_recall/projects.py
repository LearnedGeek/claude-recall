"""Project-slug helpers (v0.2.1).

Claude Code stores session archives under
``~/.claude/projects/<project-slug>/<uuid>.jsonl`` where ``<project-slug>`` is
derived from the working directory Claude Code was launched in. Historically
the convention has drifted between ``E--...`` and ``e--...`` forms; the current
convention is lowercase drive letter. This module computes the canonical slug
from a path and, when a DB connection is supplied, resolves it against the
sessions table case-insensitively so both conventions work.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def slug_from_path(path: Path | str) -> str:
    """Derive the Claude Code project slug from an absolute directory path.

    Rules (matching the Claude Code convention observed in real archives):
    - Drive letter on Windows is lowercased (``E:\\Documents\\...`` → ``e--Documents-...``)
    - ``:`` becomes ``-``
    - ``\\`` and ``/`` each become ``-``
    - The path is not URL- or percent-encoded; unusual characters pass through

    This is a pure function. No filesystem access. No DB lookup.
    """
    s = str(path)
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        s = s[0].lower() + s[1:]
    return s.replace(":", "-").replace("\\", "-").replace("/", "-")


def resolve_project_slug(
    conn: sqlite3.Connection, cwd: Path | str | None = None
) -> str | None:
    """Resolve the project_slug that corresponds to ``cwd`` in the index.

    Returns the exact slug stored in ``sessions.project_slug`` if a case-
    insensitive match exists, else the canonical (lowercase) slug derived
    from the path, else None if cwd is empty.

    The DB lookup lets auto-scoping work for users whose archive still uses
    the older ``E--`` uppercase convention — we return the actual stored
    slug so the ``WHERE project_slug = ?`` filter in the search SQL hits.
    """
    if cwd is None:
        cwd = Path.cwd()
    canonical = slug_from_path(Path(cwd).absolute())
    if not canonical:
        return None

    row = conn.execute(
        "SELECT project_slug FROM sessions "
        "WHERE LOWER(project_slug) = LOWER(?) LIMIT 1",
        (canonical,),
    ).fetchone()
    if row is not None:
        return row["project_slug"]
    return canonical
