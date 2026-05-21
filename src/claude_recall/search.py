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
    # Populated only when semantic rerank ran; None otherwise.
    bm25_rank: int | None = None
    semantic_rank: int | None = None


@dataclass
class SearchResponse:
    query: str
    total_matches: int
    returned: int
    threshold: float
    results: list[SearchResult] = field(default_factory=list)
    semantic_used: bool = False
    semantic_fallback_reason: str | None = None


def run_search(
    conn: sqlite3.Connection,
    query: str,
    days: int = 90,
    limit: int = 10,
    project_slug: str | None = None,
    threshold: float = 0.0,
    extract_keywords: bool = False,
    semantic: bool = False,
    ollama_client=None,
    rerank_pool_size: int = 50,
    cross_project_boost: bool = False,
    kinds: list[str] | None = None,
) -> SearchResponse:
    """Execute an FTS5 search (optionally semantic-reranked) and return results.

    BM25 scores are returned from FTS5 as negative-ish numbers where smaller is
    more relevant. We negate so higher-is-better for the user-facing score, and
    filter ``score >= threshold``.

    When ``extract_keywords`` is True, natural-language tokens (stopwords,
    pronouns, fillers) are stripped before the query reaches FTS5 and the
    remaining keywords are OR-joined with BM25 ranking. This is the v0.2 hook
    path. Direct CLI users leave it off to preserve exact-query semantics.

    When ``semantic=True`` AND ``ollama_client`` is supplied, the top
    ``rerank_pool_size`` BM25 hits are re-ranked by cosine similarity against
    the query embedding, and the top ``limit`` are returned. Any Ollama
    failure degrades silently to FTS5-only, with the reason surfaced on
    ``SearchResponse.semantic_fallback_reason`` for the CLI to log.
    """
    if extract_keywords:
        extracted = _keywords.extract_keywords(query)
        fts_input = _keywords.build_fts_query(extracted) or query
    else:
        fts_input = query

    # When semantic rerank is requested we need more candidates than the final
    # limit so the rerank has something to work with. When disabled, the old
    # behavior (no SQL LIMIT; apply Python-side) is preserved.
    pool = max(limit, rerank_pool_size) if semantic and ollama_client else None

    cutoff_iso = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    snippet_expr = (
        f"snippet(messages_fts, 0, '{_SNIPPET_START}', '{_SNIPPET_END}', "
        f"'{_SNIPPET_TRUNC}', {_SNIPPET_TOKENS})"
    )

    sql = f"""
        SELECT
            m.msg_id,
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
          AND (m.timestamp IS NULL OR m.timestamp >= ?)
    """
    # Issue #13: per-message timestamp filter, not per-session start time.
    # The previous predicate (`s.started_at >= ?`) excluded entire long-lived
    # sessions whose first message was older than the cutoff — even when
    # individual messages within them were recent. Diagnosed against OC's
    # CrewTrack archive (5,622-turn single session spanning many months;
    # default `--days 90` returned zero hits despite 139 fresh "dashboard"
    # matches in the unscoped query). `m.timestamp` is the semantically
    # correct column — `--days N` should mean "messages from the last N
    # days," and the existing idx_messages_timestamp index keeps it cheap.
    tail_params: list = [cutoff_iso]
    if project_slug:
        # Case-insensitive match so callers can pass either the canonical
        # lowercase form (`e--Documents-dev-Foo`) or the legacy Claude Code
        # uppercase form (`E--Documents-dev-Foo`) and still find matches
        # against whatever case is actually stored. The `--project auto`
        # path already normalizes via projects.resolve_project_slug; the
        # explicit form previously did not, producing the surprising
        # "list shows it but search returns nothing" symptom.
        sql += " AND LOWER(s.project_slug) = LOWER(?)"
        tail_params.append(project_slug)
    if kinds:
        # v0.8 (issue #27): scope to caller-specified content kinds.
        # NULL content_kind values are included alongside the requested
        # kinds so partially-classified DBs (interrupted v3 migration,
        # legacy archives) don't silently lose hits — same NULL-tolerance
        # discipline `topics` uses.
        placeholders = ",".join("?" * len(kinds))
        sql += f" AND (m.content_kind IS NULL OR m.content_kind IN ({placeholders}))"
        tail_params.extend(kinds)
    sql += " ORDER BY bm25(messages_fts) ASC"
    if pool is not None:
        sql += f" LIMIT {int(pool)}"

    rows = _execute_with_fallback(conn, sql, fts_input, tail_params)

    results: list[SearchResult] = []
    msg_ids: list[int] = []
    for row in rows:
        score = float(row["score"])
        if score < threshold:
            continue
        preview = _build_preview(row["content"])
        msg_ids.append(int(row["msg_id"]))
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

    semantic_used = False
    fallback_reason: str | None = None
    if semantic and ollama_client and results:
        # When the caller scoped to a single project, the boost is
        # meaningless (every result is from the same project) and would
        # waste a Counter pass. Silently no-op rather than warn — mirrors
        # how `--semantic` itself silently degrades.
        boost = cross_project_boost and project_slug is None
        reranked, fallback_reason = _semantic_rerank(
            conn, ollama_client, fts_input, msg_ids, results,
            cross_project_boost=boost,
        )
        if reranked is not None:
            results = reranked
            semantic_used = True

    total = len(results)
    returned = results[:limit]
    return SearchResponse(
        query=query,
        total_matches=total,
        returned=len(returned),
        threshold=threshold,
        results=returned,
        semantic_used=semantic_used,
        semantic_fallback_reason=fallback_reason,
    )


def _semantic_rerank(
    conn: sqlite3.Connection,
    client,
    query_text: str,
    msg_ids: list[int],
    results: list[SearchResult],
    *,
    cross_project_boost: bool = False,
) -> tuple[list[SearchResult] | None, str | None]:
    """Rerank ``results`` by cosine vs. the query embedding.

    Returns (reranked_results, fallback_reason). If the semantic path fails
    (no vectors, Ollama down, shape mismatch), returns (None, reason) so the
    caller leaves the FTS5-only order in place and logs the reason.

    When ``cross_project_boost`` is True (v0.7.2), results from projects
    that contributed multiple hits get a multiplicative score nudge:
    ``1.05× per additional same-project hit beyond the first, capped at
    1.5×``. This surfaces themes that recur across projects above
    same-project repetition without overpowering a clearly higher-scoring
    single hit. No-op silently when the candidate pool spans only one
    project.
    """
    try:
        from . import embeddings as _embeddings
    except ImportError as exc:
        return None, f"[embeddings] extra not installed: {exc}"

    # Load vectors for the pool. Missing rows keep their BM25 position.
    placeholders = ",".join("?" for _ in msg_ids)
    vec_rows = conn.execute(
        f"SELECT msg_id, vector, dim FROM message_vectors "
        f"WHERE msg_id IN ({placeholders})",
        msg_ids,
    ).fetchall()
    if not vec_rows:
        return None, "no vectors in index; run `claude-recall embed`"

    by_id: dict[int, tuple[bytes, int]] = {
        r["msg_id"]: (r["vector"], int(r["dim"])) for r in vec_rows
    }

    # Embed the query. Failure → fallback.
    try:
        q = client.embed(query_text)
    except _embeddings.EmbeddingError as exc:
        return None, f"Ollama embed failed: {exc}"

    import numpy as np

    # Build candidate matrix in result order; vectorless rows get -inf so
    # they sort to the bottom on cosine but remain in the return set.
    dim = int(q.shape[0])
    indexed: list[tuple[int, float]] = []  # (original_idx, cosine_or_neg_inf)
    vectors: list[np.ndarray] = []
    kept_idx: list[int] = []
    for i, mid in enumerate(msg_ids):
        entry = by_id.get(mid)
        if entry is None:
            indexed.append((i, float("-inf")))
            continue
        blob, stored_dim = entry
        if stored_dim != dim:
            indexed.append((i, float("-inf")))
            continue
        vectors.append(_embeddings.unpack_vector(blob, dim))
        kept_idx.append(i)

    if vectors:
        matrix = np.stack(vectors)
        scores = _embeddings.cosine_matrix(q, matrix)
        for k, orig_i in enumerate(kept_idx):
            indexed.append((orig_i, float(scores[k])))

    # Cross-project boost (v0.7.2). Apply on cosine score before the final
    # sort so the boost competes inside the same ranker the user would
    # otherwise see. We only nudge when the candidate pool actually spans
    # multiple projects — in a single-project pool there's nothing to
    # promote, and the project-filter path passes through unchanged.
    if cross_project_boost:
        from collections import Counter
        pool_projects = {results[i].project_slug for i, _ in indexed}
        if len(pool_projects) >= 2:
            proj_counts = Counter(results[i].project_slug for i, _ in indexed)
            boosted: list[tuple[int, float]] = []
            for orig_i, score in indexed:
                if score == float("-inf"):
                    # Vectorless rows stay at the bottom; no boost.
                    boosted.append((orig_i, score))
                    continue
                slug = results[orig_i].project_slug
                # 1.05× per additional same-project hit beyond the first,
                # capped at 1.5× total. 1 hit → 1.0×, 2 → 1.05×, 5 → 1.20×,
                # 10+ → 1.50×. Multiplicative on cosine — nudges adjacent
                # ranks without flipping clear winners.
                multiplier = min(1.0 + 0.05 * max(0, proj_counts[slug] - 1), 1.5)
                boosted.append((orig_i, score * multiplier))
            indexed = boosted

    # Sort indexed by semantic score desc, stable; record original bm25 rank.
    indexed.sort(key=lambda t: (-t[1], t[0]))

    reranked: list[SearchResult] = []
    for new_rank, (orig_i, _score) in enumerate(indexed):
        r = results[orig_i]
        r.bm25_rank = orig_i
        r.semantic_rank = new_rank
        reranked.append(r)
    return reranked, None


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


def build_agent_context_text(response: SearchResponse) -> str:
    """Build the plain-text additionalContext content (no JSON wrapper).

    Extracted from _format_agent_context so the hook composer (emit-prompt-
    context subcommand, issue #29) can prepend interventions content to the
    same body before wrapping. Returns empty string when no results, so
    callers can compose conditionally.
    """
    if not response.results:
        return ""
    lines = ["Relevant prior-session context (claude-recall):", ""]
    for r in response.results:
        date_part = (r.started_at or "")[:10]
        short_id = r.session_id[:8]
        clean = _strip_marks(r.snippet)
        lines.append(f"[Session {short_id}, {date_part}] {r.role}: {clean}")
    return "\n".join(lines)


def wrap_agent_context_payload(text: str) -> str:
    """Wrap pre-built additionalContext text in the canonical hookSpecificOutput
    envelope. Issue #21 (v0.6.4) covers why we must emit the wrapped shape.

    Empty input → ``"{}"`` (the hook treats this as a no-op).
    """
    if not text:
        return "{}"
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    return json.dumps(payload, ensure_ascii=False)


def _format_agent_context(response: SearchResponse) -> str:
    """Emit the Claude Code hook JSON in the canonical wrapped form.

    Schema-correct shape (per Claude Code's hook output contract):

        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                "additionalContext": "..."}}

    Issue #21 (v0.6.4): we previously emitted top-level
    ``{"additionalContext": "..."}``, which was accepted leniently by
    older Claude Code versions but is silently dropped by the strict-
    validation pass that came in alongside the v2.1.118 hook-schema
    tightening. Switched to the wrapped form so context injection
    actually reaches the model again.

    Empty ``{}`` when there are no results — the hook treats this as a no-op.
    """
    return wrap_agent_context_payload(build_agent_context_text(response))


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
