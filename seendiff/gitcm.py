"""git cm: commit what's staged, starting from a proposed message.

Installed as the `git-cm` console script, so git finds it as `git cm`.

Proposed messages are COMMIT_MSG_<description>.txt files in the repo root, one
per queued change (the same files seendiff's commit panel reads):

  git cm            one proposal waiting: open it in your editor, commit
  git cm <word>     several waiting: pick the one whose name contains <word>
  git cm -l         list the proposals

The editor opens with the proposal filled in; save to commit, empty it to
abort. '#' lines are dropped. On success the proposal file is deleted: the
message lives in the commit now. Arguments after `--` go to git commit, e.g.
`git cm -- --no-verify`.
"""
from __future__ import annotations

import os
import subprocess
import sys

from . import git as G


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            return line.strip()
    return ""


def _list(root: str, msgs: list[dict], out=None) -> None:
    out = out or sys.stdout
    if not msgs:
        print(f"no proposed messages (COMMIT_MSG_*.txt) in {root}", file=out)
    for m in msgs:
        print(f"  {m['name']:<40} {_first_line(m['text'])}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in args:
        i = args.index("--")
        args, extra = args[:i], args[i + 1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__.split("\n\n", 1)[1].strip())
        return 0
    listing = any(a in ("-l", "--list") for a in args)
    words = [a for a in args if a not in ("-l", "--list")]
    if len(words) > 1 or any(w.startswith("-") for w in words):
        print("git cm: usage: git cm [-l] [<word>] [-- <git commit args>]",
              file=sys.stderr)
        return 2

    try:
        root = G.repo_root(".")
    except G.GitError as e:
        print(f"git cm: {e}", file=sys.stderr)
        return 1
    msgs = G.commit_messages(root)
    if listing:
        _list(root, msgs)
        return 0

    pick = words[0] if words else None
    if pick:
        msgs = [m for m in msgs if pick in m["name"]]
    if not msgs:
        print("git cm: no proposed message"
              + (f" matching '{pick}'" if pick else "") + "; use git commit",
              file=sys.stderr)
        return 1
    if len(msgs) > 1:
        print("git cm: several proposals"
              + (f" match '{pick}'" if pick else "") + "; name one:",
              file=sys.stderr)
        _list(root, msgs, sys.stderr)
        return 1
    name = msgs[0]["name"]

    if not G.staged_changes(root):
        print(f"git cm: nothing is staged; git add what {name} describes first",
              file=sys.stderr)
        return 1

    # not captured: git starts the user's editor on this terminal
    proc = subprocess.run(["git", "-C", root, "commit", "--edit",
                           "--cleanup=strip", f"--file={name}", *extra])
    if proc.returncode != 0:
        return proc.returncode
    try:
        os.remove(os.path.join(root, name))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
