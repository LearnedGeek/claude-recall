"""Command-line interface entry point.

Subcommands (see docs/PLAN.md section 6):
    index        - walk the archive and update the index
    search       - query the index
    show         - fetch a single session's transcript
    list         - enumerate recent sessions
    status       - health check + index summary
    init-hooks   - wire hooks into a project's .claude/settings.json

Keep this module thin. Business logic lives in indexer.py, search.py, storage.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from . import __version__, indexer, projects, search, storage
from .config import Config, load_config

# Issue #16: vectors_coverage below this threshold trips the "vectors are
# stale" warning surfaces (status text + agent-context + init-hooks). Module-
# level so init-hooks and status share the same definition. The 5% slack
# absorbs in-flight index-vs-embed races without masking real orphan-after-
# cascade situations.
EMBED_COVERAGE_THRESHOLD = 0.95

# Issue #26 (v0.6.8): explicit marker embedded in every claude-recall-emitted
# hook command. Replaces the older filename-fragment heuristic as the primary
# identification mechanism — necessary for hooks that don't reference any
# claude-recall path (e.g., the inline-PowerShell time-injection hook).
# Defined at module scope (before any init-hooks constants) so TIME_HOOK_COMMAND
# can interpolate it at module-load time.
_CLAUDE_RECALL_MARKER = "[claude-recall managed]"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-recall",
        description="Query your Claude Code session archive.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        help="Path to config.toml. Defaults to the XDG/APPDATA location.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # topics (v0.7)
    p_topics = sub.add_parser(
        "topics",
        help="Cluster the embedding space into recurring themes.",
    )
    p_topics.add_argument(
        "--limit", type=int, default=20,
        help="Maximum number of themes to return (default: 20).",
    )
    p_topics.add_argument(
        "--min-cluster-size", type=int, default=4,
        help="Drop clusters smaller than this (default: 4).",
    )
    p_topics.add_argument(
        "--similarity-threshold", type=float, default=0.75,
        help="Cosine similarity threshold for cluster merging (default: 0.75).",
    )
    p_topics.add_argument(
        "--project",
        help="Scope to one project slug, or 'auto' to use cwd's slug.",
    )
    p_topics.add_argument(
        "--since",
        help=(
            "Time-window the input set. Accepts ISO date (2026-04-01), "
            "ISO datetime, or shorthand (7d, 4w, 6m, 1y)."
        ),
    )
    p_topics.add_argument(
        "--format", choices=["json", "text", "agent-context"], default="text",
    )

    # index
    p_index = sub.add_parser("index", help="Walk the archive and update the index.")
    p_index.add_argument("--project", help="Scope to one project slug.")
    p_index.add_argument("--archive-root", help="Override archive root path.")
    p_index.add_argument("--rebuild", action="store_true", help="Full rebuild.")
    p_index.add_argument("--verbose", action="store_true")
    # Issue #23 (v0.6.6): suppresses the auto-embed pass that runs at end
    # of index when embeddings are enabled and the new tail is small. Used
    # by the SessionStart hook (which fires on every session open and
    # can't afford the latency) and by CI/scripted runs that want strict
    # separation between index and embed steps.
    p_index.add_argument(
        "--no-embed", action="store_true",
        help="Skip the auto-embed pass that runs after small index updates."
    )

    # search
    p_search = sub.add_parser("search", help="Query the index.")
    p_search.add_argument("query")
    p_search.add_argument("--days", type=int, default=None)
    p_search.add_argument("--limit", type=int, default=None)
    p_search.add_argument(
        "--project",
        help="Exact project slug, or 'auto' to scope to the current working directory.",
    )
    p_search.add_argument("--threshold", type=float, default=None)
    p_search.add_argument("--format", choices=["json", "text"], default="text")
    p_search.add_argument("--agent-context", action="store_true")
    p_search.add_argument(
        "--extract-keywords",
        action="store_true",
        help="Strip stopwords/pronouns from a natural-language query before FTS5.",
    )
    p_search.add_argument(
        "--from-config",
        action="store_true",
        help="Use hook_days/hook_limit/hook_threshold from config.toml for unspecified flags.",
    )
    p_search.add_argument(
        "--semantic",
        action="store_true",
        help=(
            "Rerank top FTS5 candidates by cosine against an Ollama embedding. "
            "Requires [embeddings].enabled=true; otherwise soft-ignored with a warning."
        ),
    )
    p_search.add_argument(
        "--semantic-from-config",
        action="store_true",
        help=(
            "Use --semantic if [embeddings].enabled AND [embeddings].use_in_hook "
            "are both true in config.toml. Shipped hook scripts pass this flag."
        ),
    )
    p_search.add_argument(
        "--cross-project-boost",
        action="store_true",
        help=(
            "Multiplicatively boost semantic-rerank scores for projects that "
            "contributed multiple hits (1.05x per extra hit, capped 1.5x). "
            "Surfaces themes that recur across projects. Requires --semantic; "
            "silently no-op when --project is set."
        ),
    )
    p_search.add_argument(
        "--kind",
        action="append",
        choices=["THOUGHT", "PROCEDURAL", "HARNESS", "TOOL_RESULT_EMBEDDED"],
        help=(
            "Scope to one or more content kinds (issue #27). May be passed "
            "multiple times to include multiple kinds (e.g., --kind THOUGHT "
            "--kind PROCEDURAL). Defaults to all kinds — search results "
            "are not filtered by kind unless this flag is given."
        ),
    )

    # show
    p_show = sub.add_parser("show", help="Fetch a session's full transcript.")
    p_show.add_argument("session_id")
    p_show.add_argument("--format", choices=["json", "text"], default="text")
    p_show.add_argument("--turns", help="Turn range, e.g. '0-20' or '-10'.")

    # list
    p_list = sub.add_parser("list", help="Enumerate recent sessions.")
    p_list.add_argument("--project")
    p_list.add_argument("--days", type=int, default=30)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--format", choices=["json", "text"], default="text")

    # status
    p_status = sub.add_parser("status", help="Index health check.")
    p_status.add_argument(
        "--format", choices=["json", "text", "agent-context"], default="text"
    )
    p_status.add_argument(
        "--integrity-check",
        action="store_true",
        help=(
            "Run consistency queries against the index: per-project message "
            "counts, sessions vs. messages vs. messages_fts row-count "
            "agreement, orphan detection. Surfaces data-integrity issues "
            "that don't show up in normal status output."
        ),
    )

    # init-hooks
    p_init = sub.add_parser("init-hooks", help="Wire hooks into a project.")
    p_init.add_argument("--project-root", help="Defaults to cwd.")
    p_init.add_argument("--force", action="store_true")

    # embed (v0.3, opt-in)
    p_embed = sub.add_parser(
        "embed",
        help="Compute embeddings for indexed messages. Requires [embeddings] extra + Ollama.",
    )
    p_embed.add_argument(
        "--project",
        help="Scope to one project slug, or 'auto' for cwd.",
    )
    p_embed.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop all vectors in scope and re-embed from scratch.",
    )
    p_embed.add_argument(
        "--batch-size", type=int, default=None,
        help="Override [embeddings].batch_size (default 32).",
    )
    p_embed.add_argument(
        "--probe",
        action="store_true",
        help="Only test the Ollama connection + model availability; do not embed.",
    )
    p_embed.add_argument("--verbose", action="store_true")

    # migrate (v0.8.2, issue #25)
    p_migrate = sub.add_parser(
        "migrate",
        help=(
            "Relocate a project archive when the project moves on disk. "
            "Same machine only — for cross-machine moves, use index on the "
            "destination."
        ),
    )
    p_migrate.add_argument(
        "--from", dest="from_slug", required=True,
        help="Existing project slug (must match an archive directory).",
    )
    p_migrate.add_argument(
        "--to", dest="to_slug", required=True,
        help="Target project slug (must NOT already exist, unless --force).",
    )
    p_migrate.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing target archive directory (rare).",
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true",
        help="Preview the migration without making changes.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    # Issues #19 and #22: force UTF-8 encoding on stdout/stderr so non-ASCII
    # characters in output (em-dashes, arrows, ≥, etc.) don't crash on
    # Windows's default cp1252 console. The C-API path through reconfigure
    # is more reliable than PYTHONIOENCODING=utf-8 since it doesn't require
    # users to know about an env var. Guard via hasattr because some test
    # contexts replace stdout with a non-text stream that doesn't support
    # reconfigure.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # ValueError: stream already detached. OSError: rare TTY edge
                # case. Either way, fall back to whatever encoding was set —
                # output may still crash on cp1252 hosts, but at least we
                # tried, and we never let the reconfigure attempt itself
                # take down the CLI.
                pass

    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser() if args.config else None
    cfg = load_config(config_path)

    handlers = {
        "index": _cmd_index,
        "search": _cmd_search,
        "show": _cmd_show,
        "list": _cmd_list,
        "status": _cmd_status,
        "init-hooks": _cmd_init_hooks,
        "embed": _cmd_embed,
        "topics": _cmd_topics,
        "migrate": _cmd_migrate,
    }
    return handlers[args.command](args, cfg)


# --- index ------------------------------------------------------------------

def _cmd_index(args: argparse.Namespace, cfg: Config) -> int:
    archive_root = (
        Path(args.archive_root).expanduser() if args.archive_root else cfg.archive_root
    )
    if not archive_root.is_dir():
        print(
            f"archive root does not exist: {archive_root}", file=sys.stderr
        )
        return 1
    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2
    try:
        report = indexer.run_index(
            conn,
            archive_root,
            project_slug=args.project,
            rebuild=args.rebuild,
            index_tool_blocks=cfg.indexing.index_tool_blocks,
            verbose=args.verbose,
        )
    except indexer.IndexerError as exc:
        print(f"index failed: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    total_sessions = (
        report.new_sessions + report.updated_sessions + report.unchanged_sessions
    )
    print(
        f"indexed {report.new_sessions} new, "
        f"{report.updated_sessions} updated, "
        f"{report.unchanged_sessions} unchanged "
        f"({total_sessions} sessions, {report.total_messages} messages, "
        f"{report.malformed_lines} malformed lines) "
        f"in {report.elapsed_seconds:.2f}s"
    )

    # Issue #23 (v0.6.6): auto-embed the new tail when small enough. v0.6.0
    # made vectors survive routine re-ingest; this closes the last gap by
    # eliminating the manual `embed` step for the common case of routine
    # active-session index runs. Bounded at AUTO_EMBED_THRESHOLD so users
    # don't get a multi-minute surprise from a long-deferred backfill —
    # those still go through manual `claude-recall embed`.
    _maybe_auto_embed(args, cfg, report)
    return 0


AUTO_EMBED_THRESHOLD = 100


def _maybe_auto_embed(
    args: argparse.Namespace, cfg: Config, report
) -> None:
    """Run `embed` on the new tail iff all guards pass. Never raises.

    Guards:
    - `--no-embed` not passed (SessionStart hook + CI users opt out)
    - `[embeddings].enabled` true
    - new-tail size in (0, AUTO_EMBED_THRESHOLD]
    - Ollama reachable
    - the [embeddings] extra is importable

    Failure during embed is non-fatal — index already succeeded; we print a
    single-line hint to stderr and return.
    """
    if args.no_embed:
        return
    if not cfg.embeddings.enabled:
        return
    new_msgs = report.total_messages
    if new_msgs <= 0:
        return
    if new_msgs > AUTO_EMBED_THRESHOLD:
        print(
            f"  ({new_msgs} new messages exceed auto-embed threshold of "
            f"{AUTO_EMBED_THRESHOLD} — run `claude-recall embed` to "
            f"embed them)",
            file=sys.stderr,
        )
        return

    try:
        from . import embeddings as _embeddings  # noqa: F401
    except ImportError:
        # [embeddings] extra not installed; nothing to do.
        return

    if not _probe_ollama_reachable(cfg):
        print(
            f"  ({new_msgs} new message(s) — Ollama unreachable, skipping "
            f"auto-embed; run `claude-recall embed` when ready)",
            file=sys.stderr,
        )
        return

    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError:
        return  # index already succeeded; embed connection failed silently

    project_slug = args.project
    if project_slug == "auto":
        project_slug = projects.resolve_project_slug(conn)

    client = _ollama_client_factory(
        cfg.embeddings.ollama_base_url,
        cfg.embeddings.model,
        cfg.embeddings.request_timeout_seconds,
        max_input_chars=cfg.embeddings.max_input_chars,
        keep_alive=cfg.embeddings.keep_alive,
    )
    try:
        embed_report = _run_embed(
            conn,
            client,
            project_slug=project_slug,
            rebuild=False,
            model=cfg.embeddings.model,
            batch_size=cfg.embeddings.batch_size,
            verbose=args.verbose,
        )
        if args.verbose:
            print(
                f"  auto-embedded {embed_report['embedded']} message(s) "
                f"in {embed_report['elapsed']:.2f}s",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        # Index already succeeded — embed failure is non-fatal.
        print(
            f"  auto-embed failed: {exc} (run `claude-recall embed` "
            f"to retry)",
            file=sys.stderr,
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        conn.close()


# --- topics (v0.7) ----------------------------------------------------------

def _cmd_topics(args: argparse.Namespace, cfg: Config) -> int:
    try:
        from . import topics as _topics
    except ImportError as exc:
        print(
            f"claude-recall topics requires the [embeddings] extra ({exc}). "
            f"Install with: pip install 'claude-recall[embeddings]'",
            file=sys.stderr,
        )
        return 1

    if not cfg.embeddings.enabled:
        print(
            "topics requires embeddings. Set [embeddings].enabled = true in "
            "config.toml.",
            file=sys.stderr,
        )
        return 1

    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2

    project_slug = args.project
    if project_slug == "auto":
        project_slug = projects.resolve_project_slug(conn)

    try:
        since = _topics.parse_since(args.since)
    except _topics.TopicsError as exc:
        # Bad --since input is a CLI usage error, not a runtime failure;
        # exit 2 matches argparse-style usage-error convention.
        print(str(exc), file=sys.stderr)
        conn.close()
        return 2

    try:
        response = _topics.run_topics(
            conn,
            project_slug=project_slug,
            similarity_threshold=args.similarity_threshold,
            min_cluster_size=args.min_cluster_size,
            limit=args.limit,
            since=since,
        )
    except _topics.TopicsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(_topics.format_topics(response, format=args.format), end="")
    return 0


# --- migrate ----------------------------------------------------------------

def _cmd_migrate(args: argparse.Namespace, cfg: Config) -> int:
    from . import migrate as _migrate

    archive_root = Path(cfg.archive_root).expanduser()
    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2

    try:
        report = _migrate.run_migrate(
            conn,
            archive_root,
            from_slug=args.from_slug,
            to_slug=args.to_slug,
            force=args.force,
            dry_run=args.dry_run,
        )
    except _migrate.MigrateError as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(_migrate.format_report(report))
    return 0


# --- search -----------------------------------------------------------------

def _cmd_search(args: argparse.Namespace, cfg: Config) -> int:
    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 3
    try:
        days = _resolve_flag(args.days, cfg.search.hook_days, 90, args.from_config)
        limit = _resolve_flag(args.limit, cfg.search.hook_limit, 10, args.from_config)
        threshold = _resolve_flag(
            args.threshold, cfg.search.hook_threshold, 0.0, args.from_config
        )

        project_slug = args.project
        if project_slug == "auto":
            project_slug = projects.resolve_project_slug(conn)

        ollama_client = None
        semantic = bool(args.semantic)
        if args.semantic_from_config and not semantic:
            # Hook path: only opt in when both enabled AND explicitly permitted
            # for the hook. Keeps the shipped hook under the 500ms budget unless
            # the user opts in after confirming their setup.
            if cfg.embeddings.enabled and cfg.embeddings.use_in_hook:
                semantic = True
        if semantic:
            if not cfg.embeddings.enabled:
                print(
                    "--semantic: [embeddings].enabled is false; "
                    "running FTS5-only. Enable in config.toml to activate rerank.",
                    file=sys.stderr,
                )
                semantic = False
            else:
                try:
                    ollama_client = _ollama_client_factory(
                        cfg.embeddings.ollama_base_url,
                        cfg.embeddings.model,
                        cfg.embeddings.request_timeout_seconds,
                        keep_alive=cfg.embeddings.keep_alive,
                    )
                except ImportError as exc:
                    print(
                        f"--semantic: [embeddings] extra not installed ({exc}); "
                        f"running FTS5-only.",
                        file=sys.stderr,
                    )
                    semantic = False

        try:
            response = search.run_search(
                conn,
                query=args.query,
                days=days,
                limit=limit,
                project_slug=project_slug,
                threshold=threshold,
                extract_keywords=args.extract_keywords,
                semantic=semantic,
                ollama_client=ollama_client,
                rerank_pool_size=cfg.embeddings.rerank_pool_size,
                cross_project_boost=args.cross_project_boost,
                kinds=args.kind,
            )
        except search.SearchError as exc:
            print(f"invalid query: {exc}", file=sys.stderr)
            return 2
        finally:
            if ollama_client is not None:
                ollama_client.close()
        if response.semantic_fallback_reason:
            print(
                f"--semantic: falling back to FTS5 ({response.semantic_fallback_reason})",
                file=sys.stderr,
            )
        fmt = "agent-context" if args.agent_context else args.format
        print(search.format_result(response, format=fmt))
        return 0
    finally:
        conn.close()


def _resolve_flag(cli_value, config_value, default, from_config: bool):
    """CLI explicit > config (only when --from-config) > hardcoded default."""
    if cli_value is not None:
        return cli_value
    if from_config:
        return config_value
    return default


# --- show -------------------------------------------------------------------

def _cmd_show(args: argparse.Namespace, cfg: Config) -> int:
    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 1
    try:
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (args.session_id,)
        ).fetchone()
        if session is None:
            print(f"session not found: {args.session_id}", file=sys.stderr)
            return 1

        sql = (
            "SELECT turn_index, role, content, timestamp "
            "FROM messages WHERE session_id = ? ORDER BY turn_index"
        )
        messages = conn.execute(sql, (args.session_id,)).fetchall()
        if args.turns:
            lo, hi = _parse_turn_range(args.turns, total=len(messages))
            messages = messages[lo:hi]

        if args.format == "json":
            payload = {
                "session_id": session["session_id"],
                "project_slug": session["project_slug"],
                "started_at": session["started_at"],
                "ended_at": session["ended_at"],
                "turn_count": session["turn_count"],
                "messages": [
                    {
                        "turn_index": m["turn_index"],
                        "role": m["role"],
                        "content": m["content"],
                        "timestamp": m["timestamp"],
                    }
                    for m in messages
                ],
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"session {session['session_id']} ({session['project_slug']})")
            print(f"started: {session['started_at']}")
            print(f"ended:   {session['ended_at']}")
            print(f"turns:   {session['turn_count']}")
            print()
            for m in messages:
                print(f"[{m['turn_index']:>4}] {m['role']}: {m['content']}")
        return 0
    finally:
        conn.close()


def _parse_turn_range(spec: str, total: int) -> tuple[int, int]:
    """Parse a turn range spec like '0-20' or '-10'.

    Returns a (lo, hi) tuple suitable for slicing messages[lo:hi].
    """
    spec = spec.strip()
    if spec.startswith("-"):
        try:
            last_n = int(spec[1:])
        except ValueError:
            return 0, total
        return max(0, total - last_n), total
    if "-" in spec:
        a, _, b = spec.partition("-")
        try:
            lo = int(a)
            hi = int(b) + 1
        except ValueError:
            return 0, total
        return max(0, lo), min(total, hi)
    try:
        idx = int(spec)
    except ValueError:
        return 0, total
    return max(0, idx), min(total, idx + 1)


# --- list -------------------------------------------------------------------

def _cmd_list(args: argparse.Namespace, cfg: Config) -> int:
    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 1
    try:
        sql = (
            "SELECT s.session_id, s.project_slug, s.started_at, s.ended_at, "
            "s.turn_count, "
            "(SELECT content FROM messages m WHERE m.session_id = s.session_id "
            "AND m.role='user' ORDER BY m.turn_index LIMIT 1) AS first_user "
            "FROM sessions s WHERE 1=1"
        )
        params: list = []
        if args.project:
            # Case-insensitive: match whether caller passed "E--Foo" or
            # "e--foo" regardless of the case actually stored. (Issue #13
            # context: casing was NOT the root cause for CrewTrack, but
            # this is still the right behavior — both cases are valid
            # Claude Code slug forms in the wild.)
            sql += " AND LOWER(s.project_slug) = LOWER(?)"
            params.append(args.project)
        sql += " ORDER BY s.started_at DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()

        if args.format == "json":
            print(
                json.dumps(
                    [
                        {
                            "session_id": r["session_id"],
                            "project_slug": r["project_slug"],
                            "started_at": r["started_at"],
                            "ended_at": r["ended_at"],
                            "turn_count": r["turn_count"],
                            "first_user": r["first_user"],
                        }
                        for r in rows
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            if not rows:
                print("no sessions indexed.")
                return 0
            for r in rows:
                preview = (r["first_user"] or "").replace("\n", " ")[:80]
                date_part = (r["started_at"] or "")[:10]
                print(
                    f"{r['session_id'][:8]}  {date_part}  "
                    f"{r['project_slug']}  turns={r['turn_count']}  {preview}"
                )
        return 0
    finally:
        conn.close()


# --- status -----------------------------------------------------------------

def _cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    if getattr(args, "integrity_check", False):
        return _run_integrity_check(cfg)
    archive_accessible = cfg.archive_root.is_dir()
    db_path = cfg.db_path
    db_accessible = False
    schema_current = False
    fts_available = False
    schema_version: int | None = None
    total_sessions = 0
    total_messages = 0
    most_recent: str | None = None
    last_indexed: str | None = None
    db_size = 0

    vectors_indexed = 0
    messages_without_vectors = 0
    conn: sqlite3.Connection | None = None
    try:
        try:
            conn = storage.open_db(db_path)
            db_accessible = True
        except storage.StorageError:
            db_accessible = False

        if conn is not None:
            fts_available = storage.fts5_available(conn)
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            schema_version = row["v"] if row else None
            schema_current = schema_version == storage.SCHEMA_VERSION
            total_sessions = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions"
            ).fetchone()["c"]
            total_messages = conn.execute(
                "SELECT COUNT(*) AS c FROM messages"
            ).fetchone()["c"]
            most_recent = conn.execute(
                "SELECT MAX(ended_at) AS m FROM sessions"
            ).fetchone()["m"]
            last_indexed = conn.execute(
                "SELECT MAX(indexed_at) AS m FROM sessions"
            ).fetchone()["m"]
            vectors_indexed = conn.execute(
                "SELECT COUNT(*) AS c FROM message_vectors"
            ).fetchone()["c"]
            messages_without_vectors = max(0, total_messages - vectors_indexed)
            if db_path.exists():
                db_size = db_path.stat().st_size
    finally:
        if conn is not None:
            conn.close()

    installed_hook_version = _read_installed_hook_version()
    hooks_stale = (
        installed_hook_version is not None
        and installed_hook_version != __version__
    )

    embeddings_enabled = cfg.embeddings.enabled
    # Issue #5: probe Ollama even when [embeddings].enabled is false so the
    # standalone `ollama_reachable` field is diagnostically honest in
    # json/text output. For agent-context (the SessionStart hook output
    # path), skip the probe when embeddings are off — the hook has a
    # latency budget and there's nothing useful to say about Ollama in
    # the one-line summary when the feature toggle is off anyway.
    if args.format == "agent-context" and not embeddings_enabled:
        ollama_reachable = False
    else:
        ollama_reachable = _probe_ollama_reachable(cfg)
    # Issue #16: vectors_indexed > 0 was too lenient — reported `ready` at
    # 16% coverage when search returned "no vectors" because the surviving
    # vectors didn't intersect the FTS5 candidate pool. Require ≥95% coverage
    # to call ourselves ready. The 5% slack absorbs in-flight index-vs-embed
    # races (a session indexed seconds ago hasn't been embedded yet) without
    # masking the real orphan-after-cascade case (#16's 16% surface).
    if total_messages > 0:
        vectors_coverage = vectors_indexed / total_messages
    else:
        vectors_coverage = 1.0
    embeddings_ready = (
        embeddings_enabled
        and ollama_reachable
        and vectors_indexed > 0
        and vectors_coverage >= EMBED_COVERAGE_THRESHOLD
    )

    payload = {
        "archive_root": str(cfg.archive_root),
        "db_path": str(db_path),
        "db_size_bytes": db_size,
        "schema_version": schema_version,
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "most_recent_session": most_recent,
        "last_indexed_at": last_indexed,
        "package_version": __version__,
        "installed_hook_version": installed_hook_version,
        "embeddings_enabled": embeddings_enabled,
        "ollama_reachable": ollama_reachable,
        "vectors_indexed": vectors_indexed,
        "messages_without_vectors": messages_without_vectors,
        "vectors_coverage": round(vectors_coverage, 4),
        "checks": {
            "archive_accessible": archive_accessible,
            "db_accessible": db_accessible,
            "schema_current": schema_current,
            "fts_available": fts_available,
            "hooks_current": not hooks_stale,
            "embeddings_ready": embeddings_ready,
        },
    }

    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.format == "agent-context":
        if not db_accessible:
            print("claude-recall: db unavailable")
        else:
            mr = (most_recent or "")[:16]
            line = (
                f"claude-recall: {total_sessions} sessions, "
                f"{total_messages:,} messages indexed"
                + (f", most recent {mr}" if mr else "")
            )
            if embeddings_enabled:
                if embeddings_ready:
                    line += (
                        f". Embeddings: {vectors_indexed:,} vectors, "
                        f"Ollama reachable."
                    )
                elif not ollama_reachable:
                    line += ". Embeddings: Ollama unreachable."
                elif vectors_indexed == 0:
                    line += (
                        ". Embeddings: 0 vectors "
                        "(run `claude-recall embed`)."
                    )
                elif messages_without_vectors > 0:
                    # Issue #16: distinguish low coverage (orphan-after-cascade
                    # surface, semantic search broken) from a few in-flight
                    # unembedded messages. The pct in the message gives the
                    # user enough signal to know whether to embed-now or
                    # ignore-til-later.
                    pct = int(vectors_coverage * 100)
                    line += (
                        f". Embeddings: {pct}% coverage "
                        f"({vectors_indexed:,}/{total_messages:,}, "
                        f"{messages_without_vectors:,} unembedded — "
                        f"run `claude-recall embed`)."
                    )
            if hooks_stale:
                line += (
                    f". Hooks at v{installed_hook_version}, package is "
                    f"v{__version__} — run `claude-recall init-hooks --force` "
                    f"to upgrade."
                )
            print(line)
    else:
        print(f"archive_root:         {payload['archive_root']}")
        print(f"db_path:              {payload['db_path']}")
        print(f"db_size_bytes:        {payload['db_size_bytes']}")
        print(f"schema_version:       {payload['schema_version']}")
        print(f"total_sessions:       {payload['total_sessions']}")
        print(f"total_messages:       {payload['total_messages']}")
        print(f"most_recent_session:  {payload['most_recent_session']}")
        print(f"last_indexed_at:      {payload['last_indexed_at']}")
        print(f"package_version:      {payload['package_version']}")
        print(f"installed_hook_version: {payload['installed_hook_version']}")
        print(f"embeddings_enabled:   {payload['embeddings_enabled']}")
        print(f"ollama_reachable:     {payload['ollama_reachable']}")
        print(f"vectors_indexed:      {payload['vectors_indexed']}")
        print(f"messages_without_vectors: {payload['messages_without_vectors']}")
        print(f"vectors_coverage:     {payload['vectors_coverage']:.2%}")
        print("checks:")
        for k, v in payload["checks"].items():
            print(f"  {k}: {v}")
        if hooks_stale:
            print(
                f"\nhooks are stale: v{installed_hook_version} installed, "
                f"v{__version__} is current. Run `claude-recall init-hooks --force`."
            )
        # Issue #16: surface the orphan-vector situation prominently when
        # we're embeddings-enabled but coverage has dropped below the
        # threshold. Mirrors the hooks-stale block — it's the diagnostic
        # the user needs to find without having to manually do the math.
        if (
            embeddings_enabled
            and ollama_reachable
            and total_messages > 0
            and vectors_coverage < EMBED_COVERAGE_THRESHOLD
        ):
            print(
                f"\nvectors are stale: {messages_without_vectors:,} of "
                f"{total_messages:,} messages have no embedding "
                f"({vectors_coverage:.1%} coverage). Routine indexing "
                f"orphans vectors via FK CASCADE on session re-ingest "
                f"(see issue #16); run `claude-recall embed` to restore "
                f"semantic search."
            )
    return 0


# --- embed (v0.3) -----------------------------------------------------------

# Injectable client factory so tests can mock the Ollama path without
# patching module-level imports. Production path always returns a real
# OllamaClient; tests override _ollama_client_factory in the module namespace.
def _ollama_client_factory(base_url: str, model: str, timeout: float,
                            max_input_chars: int | None = None,
                            keep_alive: str | None = None):
    from . import embeddings as _embeddings
    return _embeddings.OllamaClient(
        base_url=base_url,
        model=model,
        timeout=timeout,
        max_input_chars=max_input_chars,
        keep_alive=keep_alive,
    )


def _run_integrity_check(cfg: Config) -> int:
    """Diagnose index consistency. Added for issue #13 triage.

    Reports:
    - Per-project-slug: sessions count, messages count (via join), stored
      turn_count total, gap between stored turn_count and actual message
      count (the symptom for "session exists but --project filter
      returns nothing")
    - Global: messages row count vs messages_fts row count (catches
      trigger-didn't-fire case)
    - Global: sessions with zero messages despite turn_count > 0
    - Global: orphan messages (session_id pointing at missing sessions row)
    """
    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 1

    try:
        print(f"integrity check of {cfg.db_path}")
        print()

        # Global row counts
        msg_count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages"
        ).fetchone()["c"]
        fts_count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages_fts"
        ).fetchone()["c"]
        sessions_count = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions"
        ).fetchone()["c"]
        print(
            f"global: {sessions_count} sessions / {msg_count} messages / "
            f"{fts_count} messages_fts rows"
        )
        if msg_count != fts_count:
            print(
                f"  ⚠ messages ({msg_count}) != messages_fts ({fts_count}) — "
                f"FTS trigger may have missed inserts; search will return "
                f"inconsistent results"
            )

        # Per-session: stored turn_count vs actual messages rows
        gaps = conn.execute(
            """
            SELECT s.project_slug, s.session_id, s.turn_count,
                   COUNT(m.msg_id) AS actual_msgs
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.session_id
            GROUP BY s.session_id
            HAVING s.turn_count != COUNT(m.msg_id)
            ORDER BY ABS(s.turn_count - COUNT(m.msg_id)) DESC
            """
        ).fetchall()
        if gaps:
            print()
            print(
                f"sessions where stored turn_count != actual messages "
                f"row count: {len(gaps)}"
            )
            for row in gaps[:20]:
                diff = row["turn_count"] - row["actual_msgs"]
                print(
                    f"  {row['project_slug']}  session={row['session_id'][:12]}"
                    f"  turn_count={row['turn_count']}  "
                    f"actual_msgs={row['actual_msgs']}  diff={diff:+d}"
                )
            if len(gaps) > 20:
                print(f"  ... and {len(gaps) - 20} more")
        else:
            print()
            print("per-session turn_count matches actual messages row count")

        # Per-project breakdown
        print()
        print("per-project:")
        # Issue #18: aggregate per-session FIRST via a CTE, then sum
        # per-project. The previous query joined messages directly under the
        # outer GROUP BY, producing a cartesian explosion: for a session with
        # N messages, the join produced N rows each carrying turn_count=N,
        # SUM = N². Every project on a healthy archive falsely flagged as
        # mismatched. The CTE avoids the inflation by aggregating COUNT(m)
        # per session before summing turn_count per project.
        breakdown = conn.execute(
            """
            WITH session_stats AS (
                SELECT s.session_id, s.project_slug, s.turn_count,
                       COUNT(m.msg_id) AS msgs,
                       COUNT(v.msg_id) AS vectors
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.session_id
                LEFT JOIN message_vectors v ON v.msg_id = m.msg_id
                GROUP BY s.session_id, s.project_slug, s.turn_count
            )
            SELECT project_slug,
                   COUNT(*) AS sessions,
                   SUM(turn_count) AS stored_turns,
                   SUM(msgs) AS actual_msgs,
                   SUM(vectors) AS vectors
            FROM session_stats
            GROUP BY project_slug
            ORDER BY project_slug
            """
        ).fetchall()
        for row in breakdown:
            flag = ""
            if row["stored_turns"] != row["actual_msgs"]:
                flag = "  ⚠ turn_count/messages mismatch"
            print(
                f"  {row['project_slug']:60s}  "
                f"sessions={row['sessions']:>3}  "
                f"stored_turns={row['stored_turns']:>5}  "
                f"actual_msgs={row['actual_msgs']:>5}  "
                f"vectors={row['vectors']:>5}{flag}"
            )

        # Orphan messages (session_id missing from sessions table)
        orphans = conn.execute(
            """
            SELECT COUNT(*) AS c FROM messages m
            WHERE NOT EXISTS (
                SELECT 1 FROM sessions s WHERE s.session_id = m.session_id
            )
            """
        ).fetchone()["c"]
        if orphans > 0:
            print()
            print(
                f"⚠ orphan messages (session_id not in sessions table): "
                f"{orphans}"
            )

        return 0
    finally:
        conn.close()


def _probe_ollama_reachable(cfg: Config) -> bool:
    """Best-effort reachability probe for the status command. Never raises."""
    try:
        client = _ollama_client_factory(
            cfg.embeddings.ollama_base_url,
            cfg.embeddings.model,
            min(cfg.embeddings.request_timeout_seconds, 2.0),
        )
    except ImportError:
        return False
    try:
        return client.probe().ollama_reachable
    except Exception:
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def _cmd_embed(args: argparse.Namespace, cfg: Config) -> int:
    try:
        from . import embeddings as _embeddings  # noqa: F401 — import probes [embeddings] extra
    except ImportError as exc:
        print(
            f"claude-recall embed requires the [embeddings] extra ({exc}). "
            f"Install with: pip install 'claude-recall[embeddings]'",
            file=sys.stderr,
        )
        return 1

    if not cfg.embeddings.enabled and not args.probe:
        print(
            "embeddings disabled in config. Set [embeddings].enabled = true "
            "to embed, or pass --probe to test the Ollama path.",
            file=sys.stderr,
        )
        return 1

    # Issue #7: --probe is interactive and one-shot, so give it enough
    # headroom for first-call model cold-start (nomic-embed-text loads in
    # ~4s on typical hardware). The hot-path hook timeout stays short.
    probe_timeout = max(cfg.embeddings.request_timeout_seconds, 30.0) if args.probe \
        else cfg.embeddings.request_timeout_seconds
    client = _ollama_client_factory(
        cfg.embeddings.ollama_base_url,
        cfg.embeddings.model,
        probe_timeout,
        # Issue #8: truncate oversized inputs before Ollama sees them so a
        # single long message can't fail its whole batch.
        max_input_chars=cfg.embeddings.max_input_chars,
        # Issue #11: pass keep_alive so the model stays warm across a
        # typical coding session instead of unloading every 5 minutes.
        keep_alive=cfg.embeddings.keep_alive,
    )

    if args.probe:
        try:
            result = client.probe()
        finally:
            client.close()
        # Issue #7: if embed_ok is False but reachability + model are both
        # fine, the failure is almost certainly a cold-start timeout. Make
        # the error message actionable instead of letting users think it's
        # a network problem.
        error = result.error
        if (
            not result.embed_ok
            and result.ollama_reachable
            and result.model_present
            and error is not None
            and "time" in error.lower()  # 'timed out', 'timeout'
        ):
            error = (
                f"{error} — Ollama is reachable and '{cfg.embeddings.model}' "
                f"is present. First-call model load can take several seconds; "
                f"retry `embed --probe` once the model is warm."
            )
        payload = {
            "ollama_reachable": result.ollama_reachable,
            "version": result.version,
            "model_present": result.model_present,
            "embed_ok": result.embed_ok,
            "dim": result.dim,
            "error": error,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if result.embed_ok else 2

    try:
        conn = storage.open_db(cfg.db_path)
    except storage.StorageError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        client.close()
        return 3

    project_slug = args.project
    if project_slug == "auto":
        project_slug = projects.resolve_project_slug(conn)

    batch_size = args.batch_size or cfg.embeddings.batch_size
    report = _run_embed(
        conn,
        client,
        project_slug=project_slug,
        rebuild=args.rebuild,
        model=cfg.embeddings.model,
        batch_size=batch_size,
        verbose=args.verbose,
    )
    client.close()
    conn.close()

    dropped = report.get("dropped", 0)
    summary = (
        f"embedded {report['embedded']} messages "
        f"({report['skipped']} already embedded) "
        f"in {report['elapsed']:.2f}s"
    )
    if dropped:
        summary += f"; {dropped} message(s) dropped (see stderr)"
    print(summary)
    if report["errors"]:
        print(
            f"  {report['errors']} batch(es) hit errors; per-message fallback "
            f"recovered most — see stderr",
            file=sys.stderr,
        )
        # Exit 2 only when messages were actually dropped. A batch error that
        # the fallback fully recovered from shouldn't fail the exit code.
        if dropped:
            return 2
    return 0


def _run_embed(
    conn: sqlite3.Connection,
    client,
    *,
    project_slug: str | None,
    rebuild: bool,
    model: str,
    batch_size: int,
    verbose: bool,
) -> dict:
    import time
    from datetime import UTC, datetime

    if rebuild:
        if project_slug:
            conn.execute(
                "DELETE FROM message_vectors "
                "WHERE msg_id IN (SELECT m.msg_id FROM messages m "
                "JOIN sessions s ON s.session_id = m.session_id "
                "WHERE LOWER(s.project_slug) = LOWER(?))",
                (project_slug,),
            )
        else:
            conn.execute("DELETE FROM message_vectors")
        conn.commit()

    sql = (
        "SELECT m.msg_id, m.content FROM messages m "
        "LEFT JOIN message_vectors v ON v.msg_id = m.msg_id "
        "JOIN sessions s ON s.session_id = m.session_id "
        "WHERE v.msg_id IS NULL"
    )
    params: list = []
    if project_slug:
        sql += " AND LOWER(s.project_slug) = LOWER(?)"
        params.append(project_slug)
    sql += " ORDER BY m.msg_id"

    rows = conn.execute(sql, params).fetchall()
    total_to_embed = len(rows)

    already = conn.execute(
        "SELECT COUNT(*) AS c FROM message_vectors"
    ).fetchone()["c"]

    start = time.monotonic()
    embedded = 0
    errors = 0
    dropped = 0
    now_iso = datetime.now(UTC).isoformat()
    from . import embeddings as _embeddings

    def _insert(batch_rows, matrix, dim):
        nonlocal embedded
        payload = [
            (
                r["msg_id"],
                _embeddings.pack_vector(matrix[j]),
                model,
                dim,
                now_iso,
            )
            for j, r in enumerate(batch_rows)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO message_vectors"
            "(msg_id, vector, model, dim, embedded_at) VALUES (?,?,?,?,?)",
            payload,
        )
        conn.commit()
        embedded += len(batch_rows)

    for i in range(0, total_to_embed, batch_size):
        batch = rows[i : i + batch_size]
        texts = [r["content"] for r in batch]
        try:
            matrix = client.embed_batch(texts)
            _insert(batch, matrix, int(matrix.shape[1]))
            if verbose:
                print(
                    f"[embed] {embedded}/{total_to_embed}",
                    file=sys.stderr,
                )
            continue
        except _embeddings.EmbeddingError as exc:
            errors += 1
            print(
                f"[embed] batch {i // batch_size} failed ({exc}); "
                f"retrying {len(batch)} message(s) individually",
                file=sys.stderr,
            )

        # Issue #8: per-message fallback so one bad message doesn't cost the
        # whole batch. Typical outcome: 31 singletons succeed, 1 is the
        # genuine offender and gets dropped.
        for r in batch:
            try:
                matrix = client.embed_batch([r["content"]])
                _insert([r], matrix, int(matrix.shape[1]))
            except _embeddings.EmbeddingError as exc:
                dropped += 1
                if verbose:
                    print(
                        f"[embed]   dropped msg_id={r['msg_id']}: {exc}",
                        file=sys.stderr,
                    )
        if verbose:
            print(
                f"[embed] {embedded}/{total_to_embed}  (fallback: "
                f"{len(batch) - dropped} recovered, {dropped} dropped)",
                file=sys.stderr,
            )

    return {
        "embedded": embedded,
        "skipped": already,
        "errors": errors,
        "dropped": dropped,
        "elapsed": time.monotonic() - start,
    }


# --- init-hooks -------------------------------------------------------------

HOOKS_SRC_DIR = Path(__file__).resolve().parent / "hooks"
NATIVE_SRC_DIR = Path(__file__).resolve().parent / "native"
SETTINGS_FILENAME = "settings.json"
HOOK_VERSION_FILENAME = ".claude-recall-version"

# Issue #26 (v0.6.8): inline PowerShell expression that injects current local
# time as additionalContext on every UserPromptSubmit. Solves Claude's
# chronic temporal-drift problem ("go to sleep" suggestions at 2:30pm because
# the model has no ground-truth time). Emitted only when [hooks].inject_time
# is true. The leading `$null = '[claude-recall managed]'` is a discarded
# variable assignment — no execution side-effect, but a clear marker the
# strip function uses to identify this as ours (so flipping inject_time off
# and re-running --force removes it cleanly without touching any user-added
# time hooks that lack the marker).
TIME_HOOK_COMMAND = (
    f"$null = '{_CLAUDE_RECALL_MARKER}'; "
    "@{hookSpecificOutput = @{hookEventName = 'UserPromptSubmit'; "
    "additionalContext = 'Current local time: ' + "
    "(Get-Date -Format 'dddd yyyy-MM-dd HH:mm zzz')}} "
    "| ConvertTo-Json -Compress"
)


def _native_hook_binary() -> Path | None:
    """Return the path to the bundled C# hook binary if the wheel shipped one.

    Windows wheels ship ``native/claude-recall-hook.exe``. Pure-Python wheels
    (other platforms) don't; callers fall back to the shell-hook path.
    """
    if sys.platform == "win32":
        exe = NATIVE_SRC_DIR / "claude-recall-hook.exe"
        if exe.is_file():
            return exe
    return None


def _cmd_init_hooks(args: argparse.Namespace, cfg: Config) -> int:
    project_root = Path(args.project_root).expanduser() if args.project_root else Path.cwd()
    claude_dir = project_root / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Detect what the installed wheel actually provides. The v0.4.0 bug
    # (issue #3) was that this function assumed shell scripts were always
    # present; now we check each file and surface a clear error if the
    # wheel is missing everything it needs.
    bundled_binary = _native_hook_binary()
    session_start_name = (
        "session_start.ps1" if sys.platform == "win32" else "session_start.sh"
    )
    on_prompt_name = (
        "on_prompt.ps1" if sys.platform == "win32" else "on_prompt.sh"
    )
    session_start_src = HOOKS_SRC_DIR / session_start_name
    on_prompt_src = HOOKS_SRC_DIR / on_prompt_name

    have_session_start = session_start_src.is_file()
    have_on_prompt = on_prompt_src.is_file()
    have_binary = bundled_binary is not None

    # Hard requirement: we need SOMETHING to wire as UserPromptSubmit. If the
    # installed wheel has neither the binary nor the on_prompt shell script,
    # there's nothing we can do — explain it loudly instead of crashing.
    if not have_binary and not have_on_prompt:
        print(
            "claude-recall init-hooks: installed wheel is missing hook sources.\n"
            f"  Expected one of:\n"
            f"    {NATIVE_SRC_DIR / 'claude-recall-hook.exe'}\n"
            f"    {on_prompt_src}\n"
            f"  Neither exists. Re-install from a GitHub Release wheel "
            "(see README.md install section) or rebuild locally.",
            file=sys.stderr,
        )
        return 1

    # Copy whichever sources are present.
    copied: list[Path] = []

    def _copy_if_present(src: Path, dst: Path, *, label: str) -> bool:
        if not src.is_file():
            return False
        if dst.exists() and not args.force:
            print(
                f"skipping existing {label}: {dst} (use --force to overwrite)",
                file=sys.stderr,
            )
            return True
        shutil.copyfile(src, dst)
        if sys.platform != "win32":
            dst.chmod(0o755)
        copied.append(dst)
        return True

    # SessionStart hook is always a shell script — not latency-critical.
    if have_session_start:
        _copy_if_present(
            session_start_src, hooks_dir / session_start_name,
            label="SessionStart hook",
        )
        session_start_cmd: str | None = str(hooks_dir / session_start_name)
    else:
        print(
            f"claude-recall init-hooks: {session_start_src.name} missing from wheel; "
            "SessionStart hook will not be registered.",
            file=sys.stderr,
        )
        session_start_cmd = None

    # UserPromptSubmit: prefer the binary when present, fall back to shell.
    if have_binary:
        exe_dst = hooks_dir / "claude-recall-hook.exe"
        if exe_dst.exists() and not args.force:
            print(
                f"skipping existing hook binary: {exe_dst} (use --force to overwrite)",
                file=sys.stderr,
            )
        else:
            shutil.copyfile(bundled_binary, exe_dst)
            copied.append(exe_dst)
        sidecar = NATIVE_SRC_DIR / "e_sqlite3.dll"
        if sidecar.is_file():
            shutil.copyfile(sidecar, hooks_dir / "e_sqlite3.dll")
        # Issue #26 (v0.6.8): append the --__cr-managed marker flag so the
        # strip function can identify this command as ours via marker match
        # (canonical) instead of relying on the filename-fragment fallback.
        # The binary's CliArgs.Parse silently ignores unknown flags, so this
        # is a no-op at execution time but a clear marker in the command
        # string for our scanner.
        on_prompt_cmd = f"{exe_dst} --__cr-managed"
    else:
        _copy_if_present(
            on_prompt_src, hooks_dir / on_prompt_name,
            label="UserPromptSubmit hook",
        )
        on_prompt_cmd = str(hooks_dir / on_prompt_name)

    settings_path = claude_dir / SETTINGS_FILENAME
    if settings_path.exists():
        backup = settings_path.with_suffix(".json.bak")
        shutil.copyfile(settings_path, backup)
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise ValueError("settings.json root must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            print(
                f"existing {settings_path} is not valid JSON ({exc}); "
                f"backup at {backup} — aborting.",
                file=sys.stderr,
            )
            return 1
    else:
        settings = {}

    hooks_block = settings.setdefault("hooks", {})

    # --force surgically removes only claude-recall-owned commands within
    # the managed events, then re-merges our updated commands. Issue #4
    # required wiping prior claude-recall paths (so upgrades don't keep
    # stale site-packages references); issue #20 reminded us that wiping
    # the entire event also destroys user-added sibling commands composed
    # alongside ours (e.g., a time-injection PowerShell hook). Strip
    # ours only; keep the rest.
    if args.force:
        for event in ("SessionStart", "UserPromptSubmit"):
            _strip_claude_recall_commands(hooks_block, event)

    if session_start_cmd is not None:
        _merge_hook(hooks_block, "SessionStart", session_start_cmd, matcher="startup|resume")
    _merge_hook(hooks_block, "UserPromptSubmit", on_prompt_cmd, matcher=None)

    # Issue #26 (v0.6.8): emit the time-injection hook when opted in via
    # [hooks].inject_time. The marker embedded in the command lets a future
    # `init-hooks --force` (with the flag flipped to false, or just on a
    # routine refresh) remove or re-emit cleanly without touching any
    # user-added time hooks that lack the marker.
    if cfg.hooks.inject_time:
        _merge_hook(
            hooks_block,
            "UserPromptSubmit",
            TIME_HOOK_COMMAND,
            matcher=None,
            shell="powershell",
        )

    settings_path.write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8"
    )
    (hooks_dir / HOOK_VERSION_FILENAME).write_text(
        __version__ + "\n", encoding="utf-8"
    )

    # Issue #6: scaffold a commented config template on first-time setup so
    # users don't have to triangulate INTEGRATION-GUIDE + --help to turn on
    # semantic rerank. Never touches an existing config.toml — users who
    # already have one know what they're doing.
    config_created = _scaffold_config_template()

    print(f"claude-recall: hooks installed at {hooks_dir} (v{__version__})")
    verb = "rewritten" if args.force else "merged"
    print(f"claude-recall: settings {verb} into {settings_path}")
    if config_created is not None:
        print(f"claude-recall: config template written to {config_created}")

    # Suppress first-install nudges when the index + embeddings state shows
    # this is clearly an upgrade of a fully-working setup (OC's bonus note).
    has_index = _db_has_any_sessions(cfg.db_path)
    if not has_index:
        print("claude-recall: run `claude-recall index` once to build the index.")
    if not cfg.embeddings.enabled:
        print(
            "claude-recall: to enable semantic rerank, edit the [embeddings] "
            "section of your config.toml (see INTEGRATION-GUIDE §9 "
            "\"How do I turn on embeddings?\")."
        )

    # Issue #16 (DC suggestion): mirror status's stale-vectors warning here
    # so a user who upgrades and re-runs init-hooks discovers the embed step
    # without having to consult status separately. Auto-running embed
    # itself isn't safe — it's multi-minute work users wouldn't expect from
    # init-hooks — but the diagnostic surface is free.
    if cfg.embeddings.enabled and has_index:
        coverage = _compute_vectors_coverage(cfg.db_path)
        if coverage is not None and coverage < EMBED_COVERAGE_THRESHOLD:
            print(
                f"\nclaude-recall: vectors_coverage is {coverage:.1%} "
                f"(below {int(EMBED_COVERAGE_THRESHOLD * 100)}% threshold). "
                f"Run `claude-recall embed` to restore semantic search.",
                file=sys.stderr,
            )

    return 0


def _db_has_any_sessions(db_path: Path) -> bool:
    """Quick check: does the configured DB already have indexed data?

    Used to silence first-install nudges during upgrade runs of init-hooks.
    Fails closed (returns False) on any error so the nudges still fire if
    we can't tell.
    """
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            if not row or row[0] == 0:
                return False
            row = conn.execute("SELECT COUNT(*) FROM sessions LIMIT 1").fetchone()
            return bool(row and row[0] > 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _compute_vectors_coverage(db_path: Path) -> float | None:
    """Return vectors_indexed / total_messages, or None if unreadable.

    Issue #16 (v0.6): shared between `status` and `init-hooks` so both surface
    the same coverage signal. Read-only connection; fails closed so a missing
    or corrupt DB never crashes the caller. Empty archive returns 1.0 (no
    messages means no orphaned vectors — don't fire the stale-vectors
    warning during a first-install nudge).
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='messages'"
            ).fetchone()
            if not row or row[0] == 0:
                return None
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if total == 0:
                return 1.0
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='message_vectors'"
            ).fetchone()
            if not row or row[0] == 0:
                return None
            vectors = conn.execute(
                "SELECT COUNT(*) FROM message_vectors"
            ).fetchone()[0]
            return vectors / total
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _scaffold_config_template() -> Path | None:
    """Write a commented config.toml template if none exists. No-op otherwise."""
    from .config import default_config_path
    cfg_path = default_config_path()
    if cfg_path.exists():
        return None
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return cfg_path


CONFIG_TEMPLATE = '''# claude-recall configuration
#
# All keys below are optional — commented lines show the shipped defaults.
# Uncomment and edit to override.

# [archive]
# root = "~/.claude/projects"

# [database]
# path = "~/.config/claude-recall/index.db"   # %APPDATA%/claude-recall/index.db on Windows

# [search]
# hook_threshold     = 0.3
# hook_limit         = 3
# hook_days          = 30
# max_injected_tokens = 800

# [indexing]
# index_tool_blocks = false

# [hooks]
# inject_time = false                     # When true, init-hooks installs an
#                                         # additional UserPromptSubmit hook that
#                                         # injects current local time into every
#                                         # prompt. Useful when "tonight",
#                                         # "yesterday", "next week" need to
#                                         # resolve to a real wall-clock value.
#                                         # Off by default; opt in per project.

# Semantic rerank via Ollama embeddings. Off by default so the tool works
# zero-dep. To turn on:
#   1. pip install "claude-recall[embeddings] @ <wheel url>"
#   2. ollama pull nomic-embed-text
#   3. claude-recall embed --probe      # sanity-check the Ollama path
#   4. claude-recall embed              # one-time vector build (~5 min / 25k msgs)
# See INTEGRATION-GUIDE.md §9 "How do I turn on embeddings?" for details.
#
# [embeddings]
# enabled                 = true
# use_in_hook             = true           # auto-inject semantic context on every prompt
# ollama_base_url         = "http://localhost:11434"
# model                   = "nomic-embed-text"
# rerank_pool_size        = 50             # top-N FTS5 candidates sent to cosine rerank
# request_timeout_seconds = 10
# batch_size              = 32
'''


def _read_installed_hook_version(project_root: Path | None = None) -> str | None:
    """Return the version stamp written by init-hooks, or None if missing."""
    root = project_root if project_root is not None else Path.cwd()
    stamp = root / ".claude" / "hooks" / HOOK_VERSION_FILENAME
    try:
        return stamp.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# Issue #26 (v0.6.8): _CLAUDE_RECALL_MARKER is defined at module-scope top
# (so TIME_HOOK_COMMAND can interpolate it). The fragment list below stays
# as a fallback so we still recognize hooks emitted by pre-v0.6.8 versions
# that don't have the marker.
_CLAUDE_RECALL_OWNED_FRAGMENTS = (
    "claude-recall-hook",  # v0.4+ NativeAOT binary (Windows .exe + future)
    "session_start.ps1",   # SessionStart on Windows
    "session_start.sh",    # SessionStart on POSIX
    "on_prompt.ps1",       # v0.3 UserPromptSubmit fallback (Windows)
    "on_prompt.sh",        # v0.3 UserPromptSubmit fallback (POSIX)
)


def _is_claude_recall_command(command: object) -> bool:
    """Identify hooks claude-recall emitted (vs hooks the user added manually).

    Marker-first, fragments-fallback. Marker is the canonical identification
    going forward; fragments are kept for backward compat with hooks emitted
    by pre-v0.6.8 versions (no marker present). Pre-v0.6.8 users who upgrade
    and `init-hooks --force` get their hooks rewritten with markers — no
    functional change, just additive metadata.

    Load-bearing principle (issue #26): claude-recall NEVER touches a hook
    command it didn't emit. User-added hooks (no marker, no claude-recall
    fragment) pass through every `--force` cycle untouched.
    """
    if not isinstance(command, str):
        return False
    if _CLAUDE_RECALL_MARKER in command:
        return True
    return any(frag in command for frag in _CLAUDE_RECALL_OWNED_FRAGMENTS)


def _strip_claude_recall_commands(hooks_block: dict, event: str) -> None:
    """Remove claude-recall-owned commands from the event's entries while
    preserving any sibling commands the user has composed alongside ours.

    Issue #20: prior to this, `--force` did `hooks_block.pop(event)`, which
    destroyed the entire matcher entry — including any user-added commands
    like a time-injection PowerShell hook composed alongside the
    claude-recall hook within the same `hooks: [...]` array. Since v0.5.5+
    actively prompts users to run `init-hooks --force` on a stale-hook
    warning, the destruction was both silent and routine.

    The replacement: for each matcher entry under `event`, walk its inner
    `hooks: [...]` array and drop only the entries `_is_claude_recall_command`
    flags as ours. If an entry's inner array is empty after stripping,
    drop the entry. If the event has no entries left, drop the event key.
    """
    entries = hooks_block.get(event)
    if not isinstance(entries, list):
        return

    surviving: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            surviving.append(entry)
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            surviving.append(entry)
            continue
        new_inner = [
            h for h in inner
            if not (isinstance(h, dict) and _is_claude_recall_command(h.get("command")))
        ]
        if new_inner:
            preserved = dict(entry)
            preserved["hooks"] = new_inner
            surviving.append(preserved)
        # else: this entry's hooks array is empty after stripping ours; drop it.

    if surviving:
        hooks_block[event] = surviving
    else:
        hooks_block.pop(event, None)


def _merge_hook(
    hooks_block: dict,
    event: str,
    command: str,
    matcher: str | None,
    shell: str | None = None,
) -> None:
    """Insert a hook entry unless one with the same command already exists.

    Emits the nested-array shape Claude Code's settings schema requires
    (issue #15): each matcher entry contains a `hooks: [{type, command, ...}]`
    array. The flat `{command, matcher}` shape we shipped through v0.5.3 is
    rejected by the parser with "Expected array, but received undefined" and
    silently disables the host project's settings as a side effect. Bash —
    the default hook shell — also can't execute a raw `.ps1` path, so add
    `shell: "powershell"` when the command points at one (or pass `shell`
    explicitly for inline expressions like the time-injection hook from
    issue #26).
    """
    entries = hooks_block.setdefault(event, [])
    if not isinstance(entries, list):
        entries = []
        hooks_block[event] = entries
    for e in entries:
        if isinstance(e, dict):
            inner = e.get("hooks")
            if isinstance(inner, list) and any(
                isinstance(h, dict) and h.get("command") == command
                for h in inner
            ):
                return
    cmd_entry: dict = {"type": "command", "command": command}
    if shell is not None:
        cmd_entry["shell"] = shell
    elif command.lower().endswith(".ps1"):
        cmd_entry["shell"] = "powershell"
    entry: dict = {"hooks": [cmd_entry]}
    if matcher is not None:
        entry["matcher"] = matcher
    entries.append(entry)


if __name__ == "__main__":
    sys.exit(main())
