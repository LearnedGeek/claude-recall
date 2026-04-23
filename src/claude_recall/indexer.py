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

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class IndexReport:
    """Returned from run_index() so the CLI can print a summary."""

    new_sessions: int = 0
    updated_sessions: int = 0
    unchanged_sessions: int = 0
    total_messages: int = 0
    malformed_lines: int = 0
    elapsed_seconds: float = 0.0


class IndexerError(RuntimeError):
    """Raised for terminal indexer failures (missing archive root, etc.)."""


def run_index(
    conn: sqlite3.Connection,
    archive_root: Path,
    project_slug: str | None = None,
    rebuild: bool = False,
    index_tool_blocks: bool = False,
    verbose: bool = False,
) -> IndexReport:
    """Index (or re-index) the archive, returning a summary report.

    Raises IndexerError if archive_root does not exist.
    """
    archive_root = Path(archive_root).expanduser()
    if not archive_root.is_dir():
        raise IndexerError(f"archive root does not exist: {archive_root}")

    start = time.monotonic()
    report = IndexReport()

    if project_slug:
        project_dirs = [archive_root / project_slug]
        if not project_dirs[0].is_dir():
            return report
    else:
        project_dirs = [p for p in archive_root.iterdir() if p.is_dir()]

    if rebuild:
        for pdir in project_dirs:
            conn.execute(
                "DELETE FROM sessions WHERE project_slug = ?", (pdir.name,)
            )
        conn.commit()

    for pdir in project_dirs:
        slug = pdir.name
        files = sorted(pdir.glob("*.jsonl"))
        if verbose:
            print(
                f"[indexer] {slug}: {len(files)} session file(s)",
                file=sys.stderr,
            )
        for jsonl_path in files:
            _index_file(
                conn,
                jsonl_path,
                slug,
                rebuild=rebuild,
                index_tool_blocks=index_tool_blocks,
                verbose=verbose,
                report=report,
            )

    report.elapsed_seconds = time.monotonic() - start
    return report


def _index_file(
    conn: sqlite3.Connection,
    file_path: Path,
    project_slug: str,
    rebuild: bool,
    index_tool_blocks: bool,
    verbose: bool,
    report: IndexReport,
) -> None:
    abs_path = str(file_path.resolve())
    try:
        mtime = file_path.stat().st_mtime
    except OSError as exc:
        if verbose:
            print(f"[indexer] stat failed: {abs_path}: {exc}", file=sys.stderr)
        return

    existing = conn.execute(
        "SELECT session_id, file_mtime FROM sessions WHERE file_path = ?",
        (abs_path,),
    ).fetchone()

    if existing and not rebuild and existing["file_mtime"] == mtime:
        report.unchanged_sessions += 1
        return

    messages, malformed, first_ts, last_ts = _parse_session_file(
        file_path, index_tool_blocks=index_tool_blocks
    )
    report.malformed_lines += malformed

    session_id = file_path.stem
    now_iso = datetime.now(UTC).isoformat()

    # Delete any prior session row for this file (cascades to messages).
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    conn.execute(
        "INSERT INTO sessions(session_id, project_slug, file_path, file_mtime, "
        "started_at, ended_at, turn_count, indexed_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            session_id,
            project_slug,
            abs_path,
            mtime,
            first_ts,
            last_ts,
            len(messages),
            now_iso,
        ),
    )

    if messages:
        conn.executemany(
            "INSERT INTO messages(session_id, role, content, turn_index, timestamp) "
            "VALUES (?,?,?,?,?)",
            [
                (session_id, m["role"], m["content"], m["turn_index"], m["timestamp"])
                for m in messages
            ],
        )
    conn.commit()

    if existing:
        report.updated_sessions += 1
    else:
        report.new_sessions += 1
    report.total_messages += len(messages)

    if verbose:
        print(
            f"[indexer]   {file_path.name}: {len(messages)} messages"
            + (f" ({malformed} malformed)" if malformed else ""),
            file=sys.stderr,
        )


def _parse_session_file(
    file_path: Path, index_tool_blocks: bool
) -> tuple[list[dict], int, str | None, str | None]:
    """Parse a JSONL file into normalized message dicts.

    Returns (messages, malformed_line_count, first_timestamp, last_timestamp).
    Malformed or empty-content lines are skipped. Turn index is assigned to the
    messages that survive parsing, starting at 0.
    """
    messages: list[dict] = []
    malformed = 0
    first_ts: str | None = None
    last_ts: str | None = None

    try:
        fh = file_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return messages, malformed, first_ts, last_ts

    with fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            parsed = parse_jsonl_line(line, index_tool_blocks=index_tool_blocks)
            if parsed is None:
                # parse_jsonl_line returns None for both malformed JSON and
                # intentional skips (e.g. tool-only content); we only count the
                # former as malformed.
                if not _is_valid_json(line):
                    malformed += 1
                continue
            parsed["turn_index"] = len(messages)
            messages.append(parsed)
            ts = parsed.get("timestamp")
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

    return messages, malformed, first_ts, last_ts


def _is_valid_json(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except json.JSONDecodeError:
        return False


def parse_jsonl_line(line: str, index_tool_blocks: bool = False) -> dict | None:
    """Parse a single JSONL line into a normalized message dict.

    Returns None for malformed lines, empty content, or lines whose content
    contains only tool blocks when ``index_tool_blocks`` is False.

    Returned dict shape (turn_index is set by caller):
        {"role": str, "content": str, "timestamp": str | None}
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    role = _extract_role(obj)
    timestamp = _extract_timestamp(obj)
    content = _extract_content(obj, index_tool_blocks=index_tool_blocks)

    if not content or not content.strip():
        return None

    return {"role": role, "content": content, "timestamp": timestamp}


def _extract_role(obj: dict) -> str:
    # Prefer explicit role from nested message.role, then top-level role, then type.
    msg = obj.get("message")
    if isinstance(msg, dict):
        role = msg.get("role")
        if isinstance(role, str) and role:
            return role
    role = obj.get("role")
    if isinstance(role, str) and role:
        return role
    typ = obj.get("type")
    if isinstance(typ, str) and typ:
        return typ
    return "unknown"


def _extract_timestamp(obj: dict) -> str | None:
    ts = obj.get("timestamp")
    if isinstance(ts, str) and ts:
        return ts
    msg = obj.get("message")
    if isinstance(msg, dict):
        ts = msg.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


def _extract_content(obj: dict, index_tool_blocks: bool) -> str:
    raw = None
    msg = obj.get("message")
    if isinstance(msg, dict) and "content" in msg:
        raw = msg["content"]
    elif "content" in obj:
        raw = obj["content"]

    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return _flatten_blocks(raw, index_tool_blocks=index_tool_blocks)
    # Unknown content shape — fall back to repr
    return str(raw)


def _flatten_blocks(blocks: list, index_tool_blocks: bool) -> str:
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif btype == "tool_use":
            if index_tool_blocks:
                name = block.get("name", "")
                inp = block.get("input", {})
                parts.append(f"[tool_use: {name}] {json.dumps(inp, default=str)}")
        elif btype == "tool_result":
            if index_tool_blocks:
                content = block.get("content")
                if isinstance(content, str):
                    parts.append(f"[tool_result] {content}")
                elif isinstance(content, list):
                    parts.append(
                        "[tool_result] "
                        + _flatten_blocks(content, index_tool_blocks=True)
                    )
        # Unknown block types are silently skipped.
    return "\n".join(parts)
