"""CLI entry: argparse, free port, browser open, uvicorn (PLAN.md §3)."""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

import uvicorn

from . import git as G
from . import store
from .server import AppState, create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="seendiff",
        description="Point-in-time diff reviewer with persistent review state.")
    ap.add_argument("base", nargs="?", default=None,
                    help="base ref (default: origin/master, with fallbacks)")
    ap.add_argument("--fetch", action="store_true",
                    help="run git fetch before diffing (never implicit)")
    ap.add_argument("--clear", action="store_true",
                    help="clear review state for this scope, then continue")
    ap.add_argument("--no-auto-seen", action="store_true")
    ap.add_argument("--no-highlight", action="store_true")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    try:
        repo = G.repo_root(".")
        if args.fetch:
            G.fetch(repo)
        base_ref = G.resolve_base(repo, args.base)
    except G.GitError as e:
        print(f"seendiff: {e}", file=sys.stderr)
        return 1

    st = AppState(repo=repo, base_ref=base_ref,
                  no_highlight=args.no_highlight,
                  auto_seen=not args.no_auto_seen)

    if args.clear:
        db = store.open_db()
        mb = G.merge_base(repo, base_ref)
        scope = store.scope_key(repo, base_ref, G.branch_name(repo), mb)
        store.clear_scope(db, scope)
        db.close()
        print(f"cleared review state for {scope}")

    app = create_app(st)
    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}/"
    print(f"seendiff: {st.branch or 'detached'} vs {base_ref} "
          f"(merge base {st.mb_sha[:12]}) — {url}")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
