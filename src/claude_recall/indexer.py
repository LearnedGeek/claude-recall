"""JSONL archive walker and message extractor.

Responsibilities:
- Walk <archive_root>/<project_slug>/*.jsonl
- Incrementally detect changed files via mtime comparison
- Parse each line defensively (skip malformed, log count)
- Extract (role, content, timestamp) from each message
- Flatten content blocks (skip tool_use / tool_result unless config opts in)
- Upsert sessions, replace messages for changed sessions

See docs/PLAN.md section 5 (data model) and section 10 (implementation order).
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class IndexReport:
    """Returned from run_index() so CLI can print a summary."""
    new_sessions: int = 0
    updated_sessions: int = 0
    unchanged_sessions: int = 0
    total_messages: int = 0
    malformed_lines: int = 0
    elapsed_seconds: float = 0.0


def run_index(
    conn: sqlite3.Connection,
    archive_root: Path,
    project_slug: str | None = None,
    rebuild: bool = False,
    index_tool_blocks: bool = False,
    verbose: bool = False,
) -> IndexReport:
    """Index (or re-index) the archive.

    TODO(implementer):
    1. Validate archive_root exists, raise on missing
    2. Glob target .jsonl files (respect project_slug scope)
    3. For each file:
        a. Stat mtime
        b. Query sessions for existing entry
        c. If mtime unchanged and not rebuild, skip
        d. If changed: DELETE messages for session, parse file, bulk INSERT
        e. Upsert sessions row
    4. Return IndexReport

    Use transactions per file for atomicity. Handle malformed JSON lines by
    logging to stderr (if verbose) and continuing.
    """
    raise NotImplementedError("See docs/PLAN.md section 10 for implementation order.")


def parse_jsonl_line(line: str, index_tool_blocks: bool = False) -> dict | None:
    """Parse a single JSONL line into a normalized message dict.

    Returns None for lines that should be skipped (malformed, tool blocks when
    disabled, empty content).

    Returned dict shape:
        {
            "role": str,
            "content": str,  # flattened text
            "timestamp": str | None,
            "turn_index": int,  # set by caller
        }

    TODO(implementer): defensive parsing per docs/PLAN.md sections 5.3 and 5.4.
    """
    raise NotImplementedError
