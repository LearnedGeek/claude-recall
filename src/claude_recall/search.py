"""FTS5 query, BM25 ranking, snippet generation, output formatting.

Responsibilities:
- Build FTS5 queries safely (validate syntax, fall back to quoted-phrase on error)
- Rank results with BM25 (default FTS5 behavior)
- Generate snippets with <mark> around match tokens
- Format output as json | text | agent-context
- Honor --days, --project, --limit, --threshold filters

See docs/PLAN.md section 6.2 for the full search command spec.
"""

import sqlite3
from dataclasses import dataclass, field


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
) -> SearchResponse:
    """Execute an FTS5 search and return ranked results.

    TODO(implementer):
    1. Validate/sanitize FTS5 syntax; on parse error fall back to quoted-phrase
    2. Build SQL joining messages_fts MATCH ? with sessions metadata
    3. Apply BM25 ranking (ORDER BY bm25(messages_fts) ASC  -- lower is better)
    4. Filter by started_at > now - days, project_slug if set
    5. Apply threshold (note: BM25 returns negative-ish scores; see FTS5 docs)
    6. Build snippets via snippet(messages_fts, ...) or a manual excerpt
    7. Populate SearchResult list, return SearchResponse
    """
    raise NotImplementedError("See docs/PLAN.md section 10 for implementation order.")


def format_result(response: SearchResponse, format: str = "text") -> str:
    """Format a SearchResponse for output.

    Supported formats: 'text', 'json', 'agent-context'.
    See docs/PLAN.md section 6.2 for output schemas.
    """
    raise NotImplementedError
