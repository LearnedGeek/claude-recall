"""JSONL archive walker and message extractor.

Responsibilities:
- Walk <archive_root>/<project_slug>/*.jsonl
- Incrementally detect changed files via mtime comparison
- Parse each line defensively (skip malformed, log count)
- Extract (role, content, timestamp) from each message
- Flatten content blocks (skip tool_use / tool_result unless config opts in)
- Diff parsed messages against stored content_hash to surgically update
  changed messages without cascade-wiping unchanged vectors (issue #16)

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

from .storage import content_hash


@dataclass
class IndexReport:
    """Returned from run_index() so the CLI can print a summary."""

    new_sessions: int = 0
    updated_sessions: int = 0
    unchanged_sessions: int = 0
    incremental_sessions: int = 0
    deleted_sessions: int = 0
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

    # Issue #17: sweep orphan sessions whose JSONL files are gone from disk.
    # Scoped to the projects we just walked so `--project foo` doesn't delete
    # rows from project bar. The FK CASCADE from sessions.session_id handles
    # messages and message_vectors cleanup automatically.
    walked_slugs = [pdir.name for pdir in project_dirs]
    if walked_slugs:
        placeholders = ",".join("?" for _ in walked_slugs)
        candidates = conn.execute(
            f"SELECT session_id, file_path FROM sessions "
            f"WHERE project_slug IN ({placeholders})",
            walked_slugs,
        ).fetchall()
        to_delete = [
            row["session_id"]
            for row in candidates
            if not Path(row["file_path"]).is_file()
        ]
        if to_delete:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ph = ",".join("?" for _ in to_delete)
                conn.execute(
                    f"DELETE FROM sessions WHERE session_id IN ({ph})",
                    to_delete,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            report.deleted_sessions = len(to_delete)
            if verbose:
                print(
                    f"[indexer] cleaned up {len(to_delete)} orphan session(s) "
                    f"with missing JSONL files",
                    file=sys.stderr,
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

    # Fast path: mtime unchanged means content unchanged (filesystem-level
    # invariant); skip without acquiring a write lock or parsing the file.
    existing_meta = conn.execute(
        "SELECT session_id, file_mtime FROM sessions WHERE file_path = ?",
        (abs_path,),
    ).fetchone()
    if existing_meta and not rebuild and existing_meta["file_mtime"] == mtime:
        report.unchanged_sessions += 1
        return

    # Real work path. Acquire a write lock now so the read-then-write below
    # is atomic against a concurrent indexer (issue #16: SessionStart hook
    # firing in parallel with a manual `index` run otherwise produces
    # duplicate messages on race). Joining an outer transaction (e.g., from
    # run_index's rebuild DELETE) is safe — we just don't manage commit/
    # rollback in that case; the caller does.
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN IMMEDIATE")

    try:
        messages, malformed, first_ts, last_ts = _parse_session_file(
            file_path, index_tool_blocks=index_tool_blocks
        )
        report.malformed_lines += malformed
        new_hashes = [content_hash(m["content"]) for m in messages]

        session_id = file_path.stem
        now_iso = datetime.now(UTC).isoformat()

        existing = conn.execute(
            "SELECT session_id, file_mtime FROM sessions WHERE file_path = ?",
            (abs_path,),
        ).fetchone()

        if existing and not rebuild:
            # Hash-diff against the stored messages. Only DELETE rows whose
            # content actually changed (or fell out of range), so vectors
            # for untouched messages survive via the FK CASCADE NOT firing.
            old_rows = conn.execute(
                "SELECT msg_id, turn_index, content_hash FROM messages "
                "WHERE session_id = ? ORDER BY turn_index",
                (session_id,),
            ).fetchall()

            to_delete: list[int] = []
            surviving_indices: set[int] = set()
            for old in old_rows:
                idx = old["turn_index"]
                if idx >= len(messages):
                    # Truncation or compaction shrunk the file past this index.
                    to_delete.append(old["msg_id"])
                elif old["content_hash"] != new_hashes[idx]:
                    # Content at this turn_index changed — typical compaction
                    # signature for early indices, or a mid-stream edit.
                    to_delete.append(old["msg_id"])
                else:
                    surviving_indices.add(idx)

            if to_delete:
                placeholders = ",".join("?" * len(to_delete))
                conn.execute(
                    f"DELETE FROM messages WHERE msg_id IN ({placeholders})",
                    to_delete,
                )

            new_inserts = [
                (
                    session_id,
                    m["role"],
                    m["content"],
                    idx,
                    m["timestamp"],
                    new_hashes[idx],
                )
                for idx, m in enumerate(messages)
                if idx not in surviving_indices
            ]
            if new_inserts:
                conn.executemany(
                    "INSERT INTO messages(session_id, role, content, "
                    "turn_index, timestamp, content_hash) "
                    "VALUES (?,?,?,?,?,?)",
                    new_inserts,
                )

            conn.execute(
                "UPDATE sessions SET file_mtime=?, ended_at=?, "
                "turn_count=?, indexed_at=? WHERE session_id=?",
                (mtime, last_ts, len(messages), now_iso, session_id),
            )

            if not to_delete and not new_inserts:
                # mtime touched but content actually unchanged (atomic-write
                # rewrite, backup-restore clock skew, touch(1), etc.)
                report.unchanged_sessions += 1
            else:
                report.incremental_sessions += 1
                report.total_messages += len(new_inserts)
        else:
            # Full re-ingest path: first-time index OR --rebuild.
            conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "INSERT INTO sessions(session_id, project_slug, file_path, "
                "file_mtime, started_at, ended_at, turn_count, indexed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
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
                    "INSERT INTO messages(session_id, role, content, "
                    "turn_index, timestamp, content_hash) "
                    "VALUES (?,?,?,?,?,?)",
                    [
                        (
                            session_id,
                            m["role"],
                            m["content"],
                            m["turn_index"],
                            m["timestamp"],
                            new_hashes[idx],
                        )
                        for idx, m in enumerate(messages)
                    ],
                )

            if existing:
                report.updated_sessions += 1
            else:
                report.new_sessions += 1
            report.total_messages += len(messages)

        if own_txn:
            conn.commit()
    except Exception:
        if own_txn:
            conn.rollback()
        raise

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
