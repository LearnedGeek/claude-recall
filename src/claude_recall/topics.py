"""Thematic clustering of the embedding space (v0.7).

Turns the existing semantic embedding store into a thematic-discovery
substrate. ``run_topics`` loads every embedded message, clusters by
cosine similarity (single-link agglomerative — no extra dep), labels
each cluster with TF-IDF distinctive terms, ranks by
``cluster_size × project_count`` so cross-project recurring themes
float to the top.

Design notes:

- **No hdbscan dependency.** A compiled C extension would have been the
  easy path, but every Windows wheel-build issue we ever surface
  through is precisely the kind of thing a compiled dep brings back.
  The lightweight single-link path here uses only numpy (already
  required by the ``[embeddings]`` extra) and produces equivalent
  output for the high-density well-separated themes we're optimizing
  for. See docs/v0.7-thematic-mining.md for the full tradeoff.

- **Cosine similarity threshold.** Default 0.75 — empirically the
  point where nomic-embed-text vectors start to feel "topically
  related" rather than "merely on-topic." Tunable via CLI flag.

- **Insertion-order single-link.** For each message vector, find the
  best-matching existing cluster centroid via vectorized matmul; merge
  if cosine ≥ threshold, else start a new cluster. O(N×C) where C is
  the cluster count. For a 50k-message archive with ~hundreds of
  clusters, finishes in single-digit seconds on a desktop.

- **TF-IDF labels** filter against ``keywords.STOPWORDS`` so corpus-
  common words ("function", "code", "the") don't dominate. The
  per-cluster top-3 distinctive terms become the label.

- **Output formats** mirror ``search.format_result``: ``text``
  (human-readable ranked list), ``json`` (full dataclass dump),
  ``agent-context`` (wrapped ``hookSpecificOutput`` envelope per
  issue #21, so the same output can be piped to a hook for context
  injection during a planning session).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

DEFAULT_SIMILARITY_THRESHOLD = 0.75
DEFAULT_MIN_CLUSTER_SIZE = 4
DEFAULT_LIMIT = 20
LABEL_TOP_TERMS = 3
SAMPLE_MESSAGES_PER_CLUSTER = 3
PREVIEW_CHARS = 200

# v0.8 (issue #27, Fix 2): suppress words from labels that appear in more
# than this fraction of clusters. The previous threshold ("appears in
# EVERY cluster") was binary and let near-universal words like "files",
# "check", "verify" survive into labels. 0.5 is the v0.8 tuning point —
# tighten further if conversational mechanics still bleed through after
# the THOUGHT-only filter (Fix 1) shrinks the pool.
LABEL_CLUSTER_UNIQUENESS_THRESHOLD = 0.5

_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9]+")

# v0.7.1 --since: shorthand `\d+[dwmy]` (days/weeks/months/years). Months
# resolve to 30 days, years to 365 days — calendar-precise quarter math is
# overkill for "what's bubbling lately" mining.
_SHORTHAND_PATTERN = re.compile(r"^(\d+)([dwmy])$")
_SHORTHAND_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


class TopicsError(RuntimeError):
    """Raised for terminal failures the CLI should catch and exit on."""


def parse_since(spec: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse a --since value into a UTC datetime cutoff.

    Accepts: ``None`` (no cutoff), ISO date (``2026-04-01``), ISO datetime
    (``2026-04-01T12:00:00``), or shorthand (``7d``, ``4w``, ``6m``, ``1y``).
    Raises :class:`TopicsError` with both accepted formats listed when the
    input matches neither — caller (CLI) translates to exit 2.
    """
    if spec is None:
        return None
    text = spec.strip()
    if not text:
        return None

    shorthand_match = _SHORTHAND_PATTERN.match(text.lower())
    if shorthand_match:
        amount = int(shorthand_match.group(1))
        unit = shorthand_match.group(2)
        days = amount * _SHORTHAND_DAYS[unit]
        ref = now or datetime.now(UTC)
        return ref - timedelta(days=days)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TopicsError(
            f"--since: cannot parse {spec!r}. Accepted formats: "
            f"ISO date (e.g., 2026-04-01), ISO datetime (e.g., "
            f"2026-04-01T12:00:00), or shorthand (e.g., 7d, 4w, 6m, 1y)."
        ) from exc
    # Naive datetimes (date-only) are treated as UTC midnight to keep the
    # downstream comparison against `messages.timestamp` (ISO with tz) coherent.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass
class ThemeSample:
    session_id: str
    project_slug: str
    started_at: str | None
    turn_index: int
    role: str
    content_preview: str


@dataclass
class Theme:
    label: str
    cluster_size: int
    project_count: int
    project_slugs: list[str]
    score: int  # cluster_size × project_count
    sample_messages: list[ThemeSample] = field(default_factory=list)


@dataclass
class TopicsResponse:
    total_messages_clustered: int
    total_clusters: int
    noise_count: int            # messages alone or in below-threshold clusters
    themes: list[Theme]
    since: str | None = None    # reserved for v0.7.1 --since
    project_slug: str | None = None


def run_topics(
    conn: sqlite3.Connection,
    *,
    project_slug: str | None = None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    limit: int = DEFAULT_LIMIT,
    since: datetime | None = None,
) -> TopicsResponse:
    """Cluster the embedded archive and return ranked themes.

    Loads ``(msg_id, content, session_id, project_slug, vector)`` joining
    ``message_vectors``, ``messages``, and ``sessions``. Skips rows whose
    vector dim doesn't match the corpus's most-common dim (defensive
    against mid-flight model swaps — same shape ``_semantic_rerank``
    already uses). Clusters via single-link agglomerative on cosine.
    Returns ``TopicsResponse`` with themes sorted by descending score.

    ``since`` (v0.7.1): when set, filters to messages whose ``timestamp``
    is on or after the cutoff. Mirrors the per-message semantics issue #13
    settled for ``search --days`` — uses the ``idx_messages_timestamp``
    index added in v0.6.0.
    """
    try:
        from . import embeddings as _emb
    except ImportError as exc:
        raise TopicsError(
            f"topics requires the [embeddings] extra ({exc}). "
            f"Install with: pip install 'claude-recall[embeddings]'"
        ) from exc
    import numpy as np

    sql = """
        SELECT m.msg_id, m.content, m.turn_index, m.role, m.timestamp,
               s.session_id, s.project_slug, s.started_at,
               v.vector, v.dim
        FROM message_vectors v
        JOIN messages m ON m.msg_id = v.msg_id
        JOIN sessions s ON s.session_id = m.session_id
    """
    # v0.8 (issue #27): scope to THOUGHT-kind messages. HARNESS,
    # PROCEDURAL, and TOOL_RESULT_EMBEDDED are pre-classified at index
    # time and excluded from the topic-mining pool. NULL handling: rows
    # whose content_kind is NULL haven't been classified yet (e.g.,
    # interrupted migration); we include them with the same opt-in
    # discipline as v0.7 to avoid silently dropping otherwise-valid
    # data, and the post-migration backfill normally fills these in.
    where_clauses: list[str] = [
        "(m.content_kind = 'THOUGHT' OR m.content_kind IS NULL)"
    ]
    params: list = []
    if project_slug:
        where_clauses.append("LOWER(s.project_slug) = LOWER(?)")
        params.append(project_slug)
    if since is not None:
        # NULL timestamps fall outside the window — they predate v0.6's
        # per-message timestamp capture and can't be confidently dated.
        where_clauses.append("m.timestamp IS NOT NULL AND m.timestamp >= ?")
        params.append(since.isoformat())
    sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY m.msg_id"

    since_iso = since.isoformat() if since is not None else None

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return TopicsResponse(
            total_messages_clustered=0,
            total_clusters=0,
            noise_count=0,
            themes=[],
            project_slug=project_slug,
            since=since_iso,
        )

    expected_dim = int(rows[0]["dim"])
    valid_indices: list[int] = []
    vectors_list: list = []
    for i, row in enumerate(rows):
        if int(row["dim"]) != expected_dim:
            continue
        try:
            vec = _emb.unpack_vector(row["vector"], expected_dim)
        except Exception:  # noqa: BLE001
            continue
        valid_indices.append(i)
        vectors_list.append(vec)

    if not vectors_list:
        return TopicsResponse(
            total_messages_clustered=0,
            total_clusters=0,
            noise_count=0,
            themes=[],
            project_slug=project_slug,
            since=since_iso,
        )

    vectors = np.stack(vectors_list).astype(np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = vectors / norms

    # Single-link agglomerative clustering. For each vector, compare against
    # all existing centroids in one matmul; merge if similarity >= threshold,
    # else start a new cluster.
    #
    # Performance note: the naive `np.stack(centroids_list)` per iteration
    # was the dominant cost at 50k+ vector scale (~5 min on OC's archive).
    # Replaced with a pre-allocated centroids matrix that grows in-place.
    # For 50k vectors this brings the clustering pass down from minutes to
    # single-digit seconds.
    n_total = normed.shape[0]
    dim = normed.shape[1]
    initial_capacity = max(64, n_total // 20)  # rough estimate; auto-grows if exceeded
    centroids = np.zeros((initial_capacity, dim), dtype=np.float32)
    n_clusters = 0
    clusters: list[list[int]] = []

    for local_idx in range(n_total):
        v = normed[local_idx]
        if n_clusters > 0:
            sims = centroids[:n_clusters] @ v
            best_idx = int(np.argmax(sims))
            best_sim = float(sims[best_idx])
        else:
            best_idx = -1
            best_sim = -1.0

        if best_sim >= similarity_threshold and best_idx >= 0:
            clusters[best_idx].append(local_idx)
            # Update centroid in-place. mean over the cluster's members,
            # then L2-normalize.
            members = normed[clusters[best_idx]]
            new_centroid = members.mean(axis=0)
            n = float(np.linalg.norm(new_centroid))
            if n > 0:
                new_centroid = new_centroid / n
            centroids[best_idx] = new_centroid
        else:
            # New cluster — append. Grow the centroids matrix if at capacity.
            if n_clusters >= centroids.shape[0]:
                new_capacity = centroids.shape[0] * 2
                grown = np.zeros((new_capacity, dim), dtype=np.float32)
                grown[:n_clusters] = centroids[:n_clusters]
                centroids = grown
            centroids[n_clusters] = v
            clusters.append([local_idx])
            n_clusters += 1

    # Build centroids_list from the grown matrix for downstream code.
    centroids_list = [centroids[i] for i in range(n_clusters)]

    # Filter to qualifying clusters; rest is "noise."
    noise_count = 0
    qualifying_clusters: list[list[int]] = []
    qualifying_centroids: list = []
    for cluster, centroid in zip(clusters, centroids_list):
        if len(cluster) < min_cluster_size:
            noise_count += len(cluster)
            continue
        qualifying_clusters.append(cluster)
        qualifying_centroids.append(centroid)

    if not qualifying_clusters:
        return TopicsResponse(
            total_messages_clustered=len(valid_indices),
            total_clusters=0,
            noise_count=noise_count,
            themes=[],
            project_slug=project_slug,
            since=since_iso,
        )

    # TF-IDF cluster labels.
    cluster_term_freqs: list[dict[str, int]] = []
    for cluster in qualifying_clusters:
        tf: dict[str, int] = {}
        for local_idx in cluster:
            row_idx = valid_indices[local_idx]
            for word in _tokenize_for_label(rows[row_idx]["content"]):
                tf[word] = tf.get(word, 0) + 1
        cluster_term_freqs.append(tf)

    doc_freq: dict[str, int] = {}
    for tf in cluster_term_freqs:
        for word in tf:
            doc_freq[word] = doc_freq.get(word, 0) + 1

    num_clusters = len(qualifying_clusters)
    # v0.8 (Fix 2): max document frequency a word may have to remain
    # eligible for a cluster label. The previous threshold filtered only
    # words appearing in EVERY cluster — too lax, conversational
    # mechanics ("check", "files", "verify") survived. With the THOUGHT
    # filter (Fix 1) already shrinking the pool, holding this at 50%
    # cluster-uniqueness gives sharp labels without starving small
    # corpora that have legitimately broad distinctive terms.
    df_max_eligible = max(1, int(num_clusters * LABEL_CLUSTER_UNIQUENESS_THRESHOLD))
    cluster_labels: list[str] = []
    for tf in cluster_term_freqs:
        scored: list[tuple[str, float]] = []
        for word, freq in tf.items():
            df = doc_freq[word]
            if df > df_max_eligible:
                # Word appears in too many clusters to be distinctive.
                continue
            idf = math.log(num_clusters / df)
            scored.append((word, freq * idf))
        scored.sort(key=lambda x: -x[1])
        top_terms = [w for w, _ in scored[:LABEL_TOP_TERMS]]
        cluster_labels.append(
            " / ".join(top_terms) if top_terms else "(no distinctive terms)"
        )

    # Build Theme objects with samples (top-K closest-to-centroid).
    themes: list[Theme] = []
    for cluster, centroid, label in zip(
        qualifying_clusters, qualifying_centroids, cluster_labels
    ):
        member_vecs = normed[cluster]
        sims_to_centroid = member_vecs @ centroid
        sample_order = np.argsort(-sims_to_centroid)[:SAMPLE_MESSAGES_PER_CLUSTER]
        samples: list[ThemeSample] = []
        for sample_pos in sample_order:
            local_idx = cluster[int(sample_pos)]
            row = rows[valid_indices[local_idx]]
            samples.append(
                ThemeSample(
                    session_id=row["session_id"],
                    project_slug=row["project_slug"],
                    started_at=row["started_at"],
                    turn_index=int(row["turn_index"]),
                    role=row["role"],
                    content_preview=_build_preview(row["content"]),
                )
            )
        slugs = sorted(
            {rows[valid_indices[li]]["project_slug"] for li in cluster}
        )
        themes.append(
            Theme(
                label=label,
                cluster_size=len(cluster),
                project_count=len(slugs),
                project_slugs=slugs,
                score=len(cluster) * len(slugs),
                sample_messages=samples,
            )
        )

    themes.sort(key=lambda t: -t.score)
    themes = themes[:limit]

    return TopicsResponse(
        total_messages_clustered=len(valid_indices),
        total_clusters=num_clusters,
        noise_count=noise_count,
        themes=themes,
        project_slug=project_slug,
        since=since_iso,
    )


def _tokenize_for_label(content: str) -> list[str]:
    """Lowercase alphanumeric tokens, length ≥ 3, non-stopword."""
    from .keywords import STOPWORDS
    return [
        tok for tok in _TOKEN_PATTERN.findall(content.lower())
        if len(tok) >= 3 and tok not in STOPWORDS
    ]


def _build_preview(content: str) -> str:
    flat = content.replace("\n", " ").strip()
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return flat[:PREVIEW_CHARS].rstrip() + "..."


def format_topics(response: TopicsResponse, format: str = "text") -> str:
    """Format a TopicsResponse for output. Mirrors search.format_result."""
    if format == "json":
        return _format_json(response)
    if format == "agent-context":
        return _format_agent_context(response)
    return _format_text(response)


def _format_json(response: TopicsResponse) -> str:
    return json.dumps(asdict(response), indent=2, ensure_ascii=False)


def _format_text(response: TopicsResponse) -> str:
    if response.total_messages_clustered == 0:
        return (
            "No messages with vectors found. Run `claude-recall embed` to "
            "embed your archive first.\n"
        )

    header_parts = [
        f"clustered {response.total_messages_clustered:,} messages "
        f"into {response.total_clusters} themes "
        f"(noise: {response.noise_count})"
    ]
    if response.since:
        # Surface the cutoff in the user-visible header — date alone is
        # the readable form (full ISO datetime in the JSON output for
        # programmatic consumers).
        header_parts.append(f"since: {response.since[:10]}")
    if response.project_slug:
        header_parts.append(f"scope: project={response.project_slug}")
    lines = [", ".join(header_parts), ""]

    if not response.themes:
        lines.append(
            "No themes met the min-cluster-size threshold. Lower "
            "--min-cluster-size or --similarity-threshold to surface more."
        )
        return "\n".join(lines) + "\n"

    for rank, theme in enumerate(response.themes, 1):
        lines.append(
            f"{rank}. [{theme.label}]  "
            f"size={theme.cluster_size}  projects={theme.project_count}  "
            f"score={theme.score}"
        )
        for sample in theme.sample_messages:
            date = (sample.started_at or "")[:10]
            short_id = sample.session_id[:8]
            lines.append(
                f"     [{short_id} {date} {sample.role}] {sample.content_preview}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_agent_context(response: TopicsResponse) -> str:
    """Wrapped hookSpecificOutput envelope per issue #21.

    Empty `{}` when there are no themes — the hook treats this as a no-op.
    """
    if not response.themes:
        return "{}"
    lines = [
        "Recurring themes from prior-session archive (claude-recall topics):",
        "",
    ]
    for rank, theme in enumerate(response.themes, 1):
        lines.append(
            f"{rank}. [{theme.label}] across {theme.project_count} project(s), "
            f"{theme.cluster_size} message(s)"
        )
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        }
    }
    return json.dumps(payload, ensure_ascii=False)
