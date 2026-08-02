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
from . import walkthrough as W
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
    ap.add_argument("--walkthrough", metavar="PATH", default=None,
                    help="open a walkthrough JSON in walkthrough mode")
    ap.add_argument("--check-walkthrough", metavar="PATH", default=None,
                    help="validate a walkthrough JSON, print diagnostics, exit 0/1")
    args = ap.parse_args(argv)

    try:
        repo = G.repo_root(".")
        if args.fetch:
            G.fetch(repo)
    except G.GitError as e:
        print(f"seendiff: {e}", file=sys.stderr)
        return 1

    try:
        base_ref = G.resolve_base(repo, args.base)
    except G.GitError as e:
        # a walkthrough of existing code doesn't need a base ref at all:
        # warn and run with an empty diff (HEAD vs HEAD)
        if not (args.walkthrough or args.check_walkthrough):
            print(f"seendiff: {e}", file=sys.stderr)
            return 1
        print(f"seendiff: {e} — walkthrough mode, using an empty diff",
              file=sys.stderr)
        base_ref = "HEAD"

    if args.check_walkthrough:
        hunk_ids = {fd.path: {b.hunk_id for b in fd.blocks}
                    for fd in G.expand_submodules(
                        repo, G.identity_diff(repo, G.merge_base(repo, base_ref)))}
        problems = W.check(args.check_walkthrough, repo, hunk_ids)
        for p in problems:
            print(p, file=sys.stderr)
        if problems:
            return 1
        wt = W.load(args.check_walkthrough, repo, hunk_ids)
        if wt is None:
            print("ok: empty walkthrough (plain diff until it's written)")
        else:
            print(f"ok: {wt.title!r} — {len(wt.acts)} acts, "
                  f"{len(wt.steps)} steps")
        return 0

    st = AppState(repo=repo, base_ref=base_ref,
                  no_highlight=args.no_highlight,
                  auto_seen=not args.no_auto_seen,
                  wt_file=args.walkthrough)

    if args.clear:
        db = store.open_db()
        mb = G.merge_base(repo, base_ref)
        scope = store.scope_key(repo, base_ref, G.branch_name(repo), mb)
        store.clear_scope(db, scope)
        db.close()
        print(f"cleared review state for {scope}")

    app = create_app(st)          # runs the initial refresh
    if args.walkthrough:
        st.load_walkthrough()     # fail loudly at load, not in the browser
        if st.wt is None and not st.wt_empty:
            print("seendiff: walkthrough failed to load:", file=sys.stderr)
            for p in (st.wt_error or "?").splitlines():
                print(f"  {p}", file=sys.stderr)
            return 1
        if st.wt is None:
            # the agent flow: open on the plain diff now, script arrives later
            print(f"seendiff: empty walkthrough — plain diff, watching "
                  f"{args.walkthrough}", file=sys.stderr)
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
