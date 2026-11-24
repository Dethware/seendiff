"""CLI entry: argparse, free port, browser open, uvicorn."""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
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
        description="Point-in-time diff tool with persistent review state.")
    ap.add_argument("base", nargs="?", default=None,
                    help="base ref (default: origin/master, with fallbacks)")
    modes = ap.add_argument_group(
        "what to compare (switchable in the UI with m / M)")
    modes.add_argument("--mode", choices=G.MODES, default=G.DEFAULT_MODE,
                       help="all: base → working tree, uncommitted work "
                       "included (default); committed: base → HEAD; "
                       "staged: HEAD → index; unstaged: index → working tree")
    for m in G.MODES[1:]:
        modes.add_argument(f"--{m}", dest="mode", action="store_const",
                           const=m, help=f"shorthand for --mode {m}")
    modes.add_argument("--no-untracked", action="store_true",
                       help="leave untracked files out of the all / unstaged "
                       "views")
    ap.add_argument("--fetch", action="store_true",
                    help="run git fetch before diffing (never implicit)")
    ap.add_argument("--clear", action="store_true",
                    help="clear review state for this scope, then continue")
    ap.add_argument("--no-auto-seen", action="store_true")
    ap.add_argument("--no-highlight", action="store_true")
    ap.add_argument("--section-min", type=int, default=G.SECTION_MIN,
                    metavar="N", help="smallest review section, in changed "
                    f"lines (default {G.SECTION_MIN})")
    ap.add_argument("--section-max", type=int, default=G.SECTION_MAX,
                    metavar="N", help="largest review section, in changed "
                    f"lines (default {G.SECTION_MAX}); a bigger block is cut "
                    "at the nearest seam in the code")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--keep-running", action="store_true",
                    help="keep serving after the last browser tab closes "
                    "(by default seendiff exits about 2 s after it)")
    ap.add_argument("--walkthrough", metavar="PATH", default=None,
                    help="open a walkthrough JSON in walkthrough mode")
    ap.add_argument("--check-walkthrough", metavar="PATH", default=None,
                    help="validate a walkthrough JSON, print diagnostics, exit 0/1")
    ap.add_argument("--list-blocks", action="store_true",
                    help="print every block of the diff (path, hunk_id, "
                    "new-side lines) for this mode and exit — the ids a "
                    "walkthrough's hunk_id refs name")
    args = ap.parse_args(argv)

    if args.section_min < 1 or args.section_max < 2 * args.section_min:
        print("seendiff: --section-max must be at least twice --section-min",
              file=sys.stderr)
        return 1

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
        # Only an explicitly named base is fatal. Without one, a repo with no
        # origin/master|main (a fresh repo, a `develop` trunk) still has
        # uncommitted work to show, and a walkthrough of existing code needs
        # no base at all: compare against HEAD.
        if args.base:
            print(f"seendiff: {e}", file=sys.stderr)
            return 1
        print(f"seendiff: {e} — using HEAD as the base", file=sys.stderr)
        base_ref = "HEAD"

    st = AppState(repo=repo, base_ref=base_ref,
                  section_min=args.section_min,
                  section_max=args.section_max,
                  no_highlight=args.no_highlight,
                  auto_seen=not args.no_auto_seen,
                  default_mode=args.mode,
                  untracked=not args.no_untracked,
                  wt_file=args.walkthrough)

    if args.list_blocks:
        for fd in st.files:
            for b in fd.blocks:
                where = (f"{b.new_start}-{b.new_end}" if b.runs
                         else "whole-file")
                print(f"{fd.path}\t{b.hunk_id}\t{where}\t"
                      f"+{b.added} -{b.removed}\t{fd.status}")
        return 0

    if args.check_walkthrough:
        # the same diff the server would show in this mode, so hunk_id refs
        # are checked against what the reader will actually see
        hunk_ids = st.hunk_ids()
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

    if args.clear:
        db = store.open_db()
        st.snapshot(st.default_mode)          # derives the scope
        store.clear_scope(db, st.scope)
        db.close()
        print(f"cleared review state for {st.scope}")

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
          f"(merge base {st.mb_sha[:12]}), showing {args.mode} — {url}")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    # Open tabs hold /api/events streams forever; without a bound, Ctrl-C would
    # wait on them. One second, then they're cut and the tabs show stale.
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        timeout_graceful_shutdown=1))
    threading.Thread(target=_supervise,
                     args=(st, server, not args.keep_running),
                     daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        # uvicorn re-raises Ctrl-C once it has shut down; uvicorn.run() used
        # to swallow it for us, Server.run() doesn't
        pass
    return 0


def _supervise(st: AppState, server: uvicorn.Server, exit_when_idle: bool) -> None:
    """Stop the server once every tab that connected has gone (Presence), and
    tell open event streams the moment shutdown starts, from either cause
    (idle, or Ctrl-C), so they end cleanly before uvicorn waits on them."""
    while not server.should_exit:
        if exit_when_idle and st.presence.idle():
            print("seendiff: last browser tab closed, exiting", file=sys.stderr)
            server.should_exit = True
            break
        time.sleep(0.2)
    st.stopping.set()


if __name__ == "__main__":
    sys.exit(main())
