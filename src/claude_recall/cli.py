"""Command-line interface entry point.

Subcommands (see docs/PLAN.md section 6):
    index        - walk the archive and update the index
    search       - query the index
    show         - fetch a single session's transcript
    list         - enumerate recent sessions
    status       - health check + index summary
    init-hooks   - wire hooks into a project's .claude/settings.json
    uninstall-hooks  - reverse of init-hooks (v0.2+)

Keep this module thin. Business logic lives in indexer.py, search.py, storage.py.
"""

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser with all subcommands wired.

    TODO(implementer): implement per the command specs in docs/PLAN.md section 6.
    """
    parser = argparse.ArgumentParser(
        prog="claude-recall",
        description="Query your Claude Code session archive.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")

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
    p_search.add_argument("--days", type=int, default=90)
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--project")
    p_search.add_argument("--threshold", type=float, default=0.0)
    p_search.add_argument("--format", choices=["json", "text"], default="text")
    p_search.add_argument("--agent-context", action="store_true")

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

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Dispatches to subcommand handlers.

    TODO(implementer): wire each subcommand to its handler. See docs/PLAN.md section 10
    for implementation order.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # TODO: dispatch to subcommand handlers. Example:
    # if args.command == "index":
    #     return indexer.run_index(args)
    # elif args.command == "search":
    #     return search.run_search(args)
    # ... etc

    print(f"claude-recall: command '{args.command}' not yet implemented", file=sys.stderr)
    print("See docs/PLAN.md for the implementation plan.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
