"""FTS5 query, BM25 ranking, snippet generation, output formatting.

Responsibilities:
- Build FTS5 queries safely (validate syntax, fall back to tokenized OR query on error)
- Rank results with BM25 (default FTS5 behavior)
- Generate snippets with <mark> around match tokens
- Format output as json | text | agent-context
- Honor --days, --project, --limit, --threshold filters

See docs/PLAN.md section 6.2 for the full search command spec.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from . import keywords as _keywords

_SNIPPET_START = "<mark>"
_SNIPPET_END = "</mark>"
_SNIPPET_TRUNC = "..."
_SNIPPET_TOKENS = 24
_PREVIEW_CHARS = 200

# Tokens we strip from sanitized FTS fallback queries.
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")


class SearchError(ValueError):
    """Raised when an FTS5 query cannot be parsed, even after the fallback."""


@dataclass
class SearchResult:
    session_id: str
    project_slug: str
    started_at: str | None
    turn_index: int
    role: str
    score: float
    snippet: str
    content_preview: str


@dataclass
class SearchResponse:
    query: str
    total_matches: int
    returned: int
    threshold: float
    results: list[SearchResult] = field(default_factory=list)


def run_search(
    conn: sqlite3.Connection,
    query: str,
    days: int = 90,
    limit: int = 10,
    project_slug: str | None = None,
    threshold: float = 0.0,
    extract_keywords: bool = False,
) -> SearchResponse:
    """Execute an FTS5 search and return ranked results.

    BM25 scores are returned from FTS5 as negative-ish numbers where smaller is
    more relevant. We negate so higher-is-better for the user-facing score, and
    filter ``score >= threshold``.

    When ``extract_keywords`` is True, natural-language tokens (stopwords,
    pronouns, fillers) are stripped before the query reaches FTS5 and the
    remaining keywords are OR-joined with BM25 ranking. This is the v0.2 hook
    path. Direct CLI users leave it off to preserve exact-query semantics.
    """
    if extract_keywords:
        extracted = _keywords.extract_keywords(query)
        fts_input = _keywords.build_fts_query(extracted) or query
    else:
        fts_input = query

    cutoff_iso = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    snippet_expr = (
        f"snippet(messages_fts, 0, '{_SNIPPET_START}', '{_SNIPPET_END}', "
        f"'{_SNIPPET_TRUNC}', {_SNIPPET_TOKENS})"
    )

    sql = f"""
        SELECT
            s.session_id,
            s.project_slug,
            s.started_at,
            m.turn_index,
            m.role,
            m.content,
            {snippet_expr} AS snippet,
            -bm25(messages_fts) AS score
        FROM messages_fts
        JOIN messages m ON m.msg_id = messages_fts.rowid
        JOIN sessions s ON s.session_id = m.session_id
        WHERE messages_fts MATCH ?
          AND (s.started_at IS NULL OR s.started_at >= ?)
    """
    tail_params: list = [cutoff_iso]
    if project_slug:
        sql += " AND s.project_slug = ?"
        tail_params.append(project_slug)
    sql += " ORDER BY bm25(messages_fts) ASC"

    rows = _execute_with_fallback(conn, sql, fts_input, tail_params)

    results: list[SearchResult] = []
    for row in rows:
        score = float(row["score"])
        if score < threshold:
            continue
        preview = _build_preview(row["content"])
        results.append(
            SearchResult(
                session_id=row["session_id"],
                project_slug=row["project_slug"],
                started_at=row["started_at"],
                turn_index=row["turn_index"],
                role=row["role"],
                score=round(score, 4),
                snippet=row["snippet"],
                content_preview=preview,
            )
        )

    total = len(results)
    returned = results[:limit]
    return SearchResponse(
        query=query,
        total_matches=total,
        returned=len(returned),
        threshold=threshold,
        results=returned,
    )


def _execute_with_fallback(
    conn: sqlite3.Connection, sql: str, query: str, tail_params: list
) -> list:
    """Execute the FTS5 query, falling back to a token-OR query.

    The fallback triggers on parse error *or* on a zero-row raw result. A
    natural-language prompt typically parses fine but AND-joins every token,
    which almost never co-occur in one message and yields no hits. Re-issuing
    as the OR-joined sanitized form catches that case while remaining
    BM25-ranked.
    """
    sanitized = _sanitize_query(query)
    attempts = [query]
    if sanitized and sanitized != query.strip():
        attempts.append(sanitized)

    last_rows: list | None = None
    for attempt in attempts:
        if not attempt or not attempt.strip():
            continue
        try:
            rows = conn.execute(sql, [attempt, *tail_params]).fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            return rows
        last_rows = rows
    if last_rows is not None:
        return last_rows
    raise SearchError(f"could not build a valid FTS5 query from: {query!r}")


def _sanitize_query(query: str) -> str:
    tokens = _TOKEN_PATTERN.findall(query)
    if not tokens:
        return ""
    return " OR ".join(f'"{tok}"' for tok in tokens)


def _build_preview(content: str) -> str:
    flat = content.replace("\n", " ").strip()
    if len(flat) <= _PREVIEW_CHARS:
        return flat
    return flat[:_PREVIEW_CHARS].rstrip() + "..."


def _strip_marks(snippet: str) -> str:
    return snippet.replace(_SNIPPET_START, "").replace(_SNIPPET_END, "")


def format_result(response: SearchResponse, format: str = "text") -> str:
    """Format a SearchResponse for output.

    Supported formats: 'text', 'json', 'agent-context'.
    See docs/PLAN.md sections 6.2 and 7.2 for output contracts.
    """
    if format == "json":
        return _format_json(response)
    if format == "agent-context":
        return _format_agent_context(response)
    return _format_text(response)


def _format_json(response: SearchResponse) -> str:
    payload = {
        "query": response.query,
        "total_matches": response.total_matches,
        "returned": response.returned,
        "threshold": response.threshold,
        "results": [asdict(r) for r in response.results],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _format_agent_context(response: SearchResponse) -> str:
    """Emit the Claude Code hook JSON: ``{"additionalContext": "..."}``.

    Empty ``{}`` when there are no results — the hook treats this as a no-op.
    """
    if not response.results:
        return "{}"
    lines = ["Relevant prior-session context (claude-recall):", ""]
    for r in response.results:
        date_part = (r.started_at or "")[:10]
        short_id = r.session_id[:8]
        clean = _strip_marks(r.snippet)
        lines.append(f"[Session {short_id}, {date_part}] {r.role}: {clean}")
    return json.dumps({"additionalContext": "\n".join(lines)}, ensure_ascii=False)


def _format_text(response: SearchResponse) -> str:
    if not response.results:
        return f"No matches for {response.query!r}."
    lines = [
        f"query: {response.query}",
        f"returned: {response.returned} / total: {response.total_matches}",
        "",
    ]
    for idx, r in enumerate(response.results, 1):
        date_part = (r.started_at or "")[:10]
        lines.append(
            f"{idx}. [{r.session_id[:8]} {date_part} turn {r.turn_index} "
            f"{r.role}] score={r.score}"
        )
        lines.append(f"   {r.snippet}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
