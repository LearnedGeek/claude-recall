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

    # index
    p_index = sub.add_parser("index", help="Walk the archive and update the index.")
    p_index.add_argument("--project", help="Scope to one project slug.")
    p_index.add_argument("--archive-root", help="Override archive root path.")
    p_index.add_argument("--rebuild", action="store_true", help="Full rebuild.")
    p_index.add_argument("--verbose", action="store_true")

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

    return parser


def main(argv: list[str] | None = None) -> int:
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
            sql += " AND s.project_slug = ?"
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
    ollama_reachable = False
    if embeddings_enabled:
        ollama_reachable = _probe_ollama_reachable(cfg)
    embeddings_ready = (
        embeddings_enabled and ollama_reachable and vectors_indexed > 0
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
                    line += (
                        f". Embeddings: {messages_without_vectors:,} "
                        f"messages unembedded (run `claude-recall embed`)."
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
        print("checks:")
        for k, v in payload["checks"].items():
            print(f"  {k}: {v}")
        if hooks_stale:
            print(
                f"\nhooks are stale: v{installed_hook_version} installed, "
                f"v{__version__} is current. Run `claude-recall init-hooks --force`."
            )
    return 0


# --- embed (v0.3) -----------------------------------------------------------

# Injectable client factory so tests can mock the Ollama path without
# patching module-level imports. Production path always returns a real
# OllamaClient; tests override _ollama_client_factory in the module namespace.
def _ollama_client_factory(base_url: str, model: str, timeout: float):
    from . import embeddings as _embeddings
    return _embeddings.OllamaClient(base_url=base_url, model=model, timeout=timeout)


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

    client = _ollama_client_factory(
        cfg.embeddings.ollama_base_url,
        cfg.embeddings.model,
        cfg.embeddings.request_timeout_seconds,
    )

    if args.probe:
        try:
            result = client.probe()
        finally:
            client.close()
        payload = {
            "ollama_reachable": result.ollama_reachable,
            "version": result.version,
            "model_present": result.model_present,
            "embed_ok": result.embed_ok,
            "dim": result.dim,
            "error": result.error,
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

    print(
        f"embedded {report['embedded']} messages "
        f"({report['skipped']} already embedded) "
        f"in {report['elapsed']:.2f}s"
    )
    if report["errors"]:
        print(f"  {report['errors']} batch(es) failed — see stderr", file=sys.stderr)
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
                "WHERE s.project_slug = ?)",
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
        sql += " AND s.project_slug = ?"
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
    now_iso = datetime.now(UTC).isoformat()
    from . import embeddings as _embeddings

    for i in range(0, total_to_embed, batch_size):
        batch = rows[i : i + batch_size]
        texts = [r["content"] for r in batch]
        try:
            matrix = client.embed_batch(texts)
        except _embeddings.EmbeddingError as exc:
            errors += 1
            print(
                f"[embed] batch {i // batch_size} failed: {exc}",
                file=sys.stderr,
            )
            continue

        dim = int(matrix.shape[1])
        to_insert = [
            (
                r["msg_id"],
                _embeddings.pack_vector(matrix[j]),
                model,
                dim,
                now_iso,
            )
            for j, r in enumerate(batch)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO message_vectors"
            "(msg_id, vector, model, dim, embedded_at) VALUES (?,?,?,?,?)",
            to_insert,
        )
        conn.commit()
        embedded += len(batch)
        if verbose:
            print(
                f"[embed] {embedded}/{total_to_embed} ({dim}-dim)",
                file=sys.stderr,
            )

    return {
        "embedded": embedded,
        "skipped": already,
        "errors": errors,
        "elapsed": time.monotonic() - start,
    }


# --- init-hooks -------------------------------------------------------------

HOOKS_SRC_DIR = Path(__file__).resolve().parent / "hooks"
NATIVE_SRC_DIR = Path(__file__).resolve().parent / "native"
SETTINGS_FILENAME = "settings.json"
HOOK_VERSION_FILENAME = ".claude-recall-version"


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

    # Detect whether the compiled hook binary shipped with this install.
    bundled_binary = _native_hook_binary()

    if sys.platform == "win32":
        script_names = ["session_start.ps1", "on_prompt.ps1"]
        session_start_cmd = str(hooks_dir / "session_start.ps1")
    else:
        script_names = ["session_start.sh", "on_prompt.sh"]
        session_start_cmd = str(hooks_dir / "session_start.sh")

    if bundled_binary is not None:
        # Fast path: C# binary replaces the on_prompt shell wrapper.
        # SessionStart still runs as a shell script (not latency-critical).
        if sys.platform == "win32":
            script_names = ["session_start.ps1"]
        else:
            script_names = ["session_start.sh"]
        on_prompt_cmd = str(hooks_dir / "claude-recall-hook.exe")
    else:
        # Pure-Python wheel: on_prompt stays a shell wrapper that shells out
        # to the Python CLI. Existing v0.3 behavior.
        if sys.platform == "win32":
            on_prompt_cmd = str(hooks_dir / "on_prompt.ps1")
        else:
            on_prompt_cmd = str(hooks_dir / "on_prompt.sh")

    for name in script_names:
        src = HOOKS_SRC_DIR / name
        dst = hooks_dir / name
        if dst.exists() and not args.force:
            print(
                f"skipping existing hook: {dst} (use --force to overwrite)",
                file=sys.stderr,
            )
            continue
        shutil.copyfile(src, dst)
        if sys.platform != "win32":
            dst.chmod(0o755)

    # Copy the native binary (and its SQLite sidecar, if present).
    if bundled_binary is not None:
        exe_dst = hooks_dir / "claude-recall-hook.exe"
        if exe_dst.exists() and not args.force:
            print(
                f"skipping existing hook binary: {exe_dst} (use --force to overwrite)",
                file=sys.stderr,
            )
        else:
            shutil.copyfile(bundled_binary, exe_dst)
        sidecar = NATIVE_SRC_DIR / "e_sqlite3.dll"
        if sidecar.is_file():
            shutil.copyfile(sidecar, hooks_dir / "e_sqlite3.dll")

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
    _merge_hook(hooks_block, "SessionStart", session_start_cmd, matcher="startup|resume")
    _merge_hook(hooks_block, "UserPromptSubmit", on_prompt_cmd, matcher=None)

    settings_path.write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8"
    )
    (hooks_dir / HOOK_VERSION_FILENAME).write_text(
        __version__ + "\n", encoding="utf-8"
    )
    print(f"claude-recall: hooks installed at {hooks_dir} (v{__version__})")
    print(f"claude-recall: settings merged into {settings_path}")
    print("claude-recall: run `claude-recall index` once to build the index.")
    return 0


def _read_installed_hook_version(project_root: Path | None = None) -> str | None:
    """Return the version stamp written by init-hooks, or None if missing."""
    root = project_root if project_root is not None else Path.cwd()
    stamp = root / ".claude" / "hooks" / HOOK_VERSION_FILENAME
    try:
        return stamp.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _merge_hook(
    hooks_block: dict, event: str, command: str, matcher: str | None
) -> None:
    """Insert a hook entry unless one with the same command already exists."""
    entries = hooks_block.setdefault(event, [])
    if not isinstance(entries, list):
        entries = []
        hooks_block[event] = entries
    for e in entries:
        if isinstance(e, dict) and e.get("command") == command:
            return
    entry: dict = {"command": command}
    if matcher is not None:
        entry["matcher"] = matcher
    entries.append(entry)


if __name__ == "__main__":
    sys.exit(main())
