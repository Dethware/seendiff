"""Git integration: ref resolution, diff invocation, parsing, block grouping.

Two invocations (see PLAN.md §4):
  - `-U0` over the whole diff, once at startup: block identity + counts.
  - `-U999999 -- <old> <new>` per file, on demand: full-file rows for display.

Blocks are defined here, not by git: a block is one or more -U0 hunks ("runs")
merged when separated by <= MERGE_GAP unchanged lines. Identity is a content
hash of the changed lines, position-independent.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field

from unidiff import PatchSet

MERGE_GAP = 3
BASE_CANDIDATES = ["origin/master", "origin/main", "master", "main"]
WHOLE_FILE_ID = "*"


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, "-c", "core.quotePath=false", *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(cwd: str = ".") -> str:
    try:
        return _git(cwd, "rev-parse", "--show-toplevel").strip()
    except GitError:
        raise GitError(f"not inside a git repository: {cwd}")


def resolve_base(repo: str, base: str | None = None) -> str:
    candidates = [base] if base else BASE_CANDIDATES
    for cand in candidates:
        proc = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--quiet", cand + "^{commit}"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return cand
    raise GitError("no base ref found; tried: " + ", ".join(candidates))


def merge_base(repo: str, base_ref: str) -> str:
    return _git(repo, "merge-base", base_ref, "HEAD").strip()


def rev_sha(repo: str, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).strip()


def commit_date(repo: str, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%cI", sha).strip()


def branch_name(repo: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", repo, "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def dirty_and_conflicted(repo: str) -> tuple[list[str], list[str]]:
    """Paths with uncommitted changes / with merge conflicts (porcelain 'U')."""
    out = _git(repo, "status", "--porcelain", "--untracked-files=no")
    dirty, conflicted = [], []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:]
        # rename entries look like "R  old -> new"
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if "U" in xy or xy in ("AA", "DD"):
            conflicted.append(path)
        else:
            dirty.append(path)
    return dirty, conflicted


def fetch(repo: str) -> None:
    _git(repo, "fetch", "--quiet")


def show_file(repo: str, sha: str, path: str) -> str:
    return _git(repo, "show", f"{sha}:{path}")


# ---------------------------------------------------------------- data model

@dataclass
class Run:
    """One -U0 hunk: a contiguous run of changed lines."""
    src_start: int
    src_len: int
    tgt_start: int
    tgt_len: int
    lines: list[tuple[str, str]]  # (line_type '+'|'-', text incl. trailing \n)

    @property
    def new_lo(self) -> int:
        return self.tgt_start

    @property
    def new_hi(self) -> int:
        # zero-length target (pure deletion) anchors at the preceding new line
        return self.tgt_start + self.tgt_len - 1 if self.tgt_len else self.tgt_start


@dataclass
class Block:
    runs: list[Run]
    hunk_id: str = ""

    @property
    def new_start(self) -> int:
        return max(1, self.runs[0].new_lo)

    @property
    def new_end(self) -> int:
        return max(1, self.runs[-1].new_hi)

    @property
    def added(self) -> int:
        return sum(1 for r in self.runs for t, _ in r.lines if t == "+")

    @property
    def removed(self) -> int:
        return sum(1 for r in self.runs for t, _ in r.lines if t == "-")

    @property
    def kind(self) -> str:
        if self.added and self.removed:
            return "mixed"
        return "add" if self.added else "del"

    def body(self) -> str:
        return "".join(f"{t}{text}" for r in self.runs for t, text in r.lines)


@dataclass
class SubInfo:
    """Marks a file that lives inside a submodule's inner diff."""
    repo: str      # absolute path of the submodule working tree
    old: str       # outer diff's old pointer (inner base)
    new: str       # outer diff's new pointer (inner rev)
    rel: str       # path relative to the submodule root
    old_rel: str | None = None


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: str  # added | deleted | modified | renamed | binary | meta | submodule
    added: int
    removed: int
    blocks: list[Block] = field(default_factory=list)
    sub: SubInfo | None = None

    @property
    def has_content(self) -> bool:
        return self.status not in ("binary", "meta")


@dataclass
class Row:
    old: int | None
    new: int | None
    type: str  # context | add | del
    text: str  # no trailing newline
    hunk_id: str | None = None


# ------------------------------------------------------------- identity pass

def _file_status(pf) -> str:
    if pf.is_binary_file:
        return "binary"
    if pf.is_added_file:
        return "added"
    if pf.is_removed_file:
        return "deleted"
    if pf.is_rename:
        return "renamed"
    return "modified"


def _strip_ab(path: str) -> str:
    for pre in ("a/", "b/"):
        if path.startswith(pre):
            return path[len(pre):]
    return path


def _group_blocks(hunks: list[Run]) -> list[Block]:
    blocks: list[Block] = []
    for run in hunks:
        if blocks and run.new_lo - blocks[-1].runs[-1].new_hi - 1 <= MERGE_GAP:
            blocks[-1].runs.append(run)
        else:
            blocks.append(Block(runs=[run]))
    return blocks


def _assign_ids(blocks: list[Block]) -> None:
    seen: dict[str, int] = {}
    for b in blocks:
        h = hashlib.sha256(b.body().encode("utf-8", "replace")).hexdigest()[:16]
        n = seen.get(h, 0) + 1
        seen[h] = n
        b.hunk_id = h if n == 1 else f"{h}-{n}"


_SUBPROJECT = "Subproject commit "


def identity_diff(repo: str, mb_sha: str, rev: str = "HEAD") -> list[FileDiff]:
    """The startup -U0 pass: every changed file with grouped, hashed blocks."""
    out = _git(repo, "diff", "--no-ext-diff", "--no-color", "--unified=0",
               "--find-renames", mb_sha, rev, "--")
    files: list[FileDiff] = []
    for pf in PatchSet(out):
        status = _file_status(pf)
        runs = [
            Run(
                src_start=h.source_start, src_len=h.source_length,
                tgt_start=h.target_start, tgt_len=h.target_length,
                lines=[(l.line_type, l.value) for l in h if l.line_type in "+-"],
            )
            for h in pf
        ]
        blocks = _group_blocks(runs)
        _assign_ids(blocks)
        fd = FileDiff(
            path=_strip_ab(pf.path),
            old_path=_strip_ab(pf.source_file) if pf.is_rename else None,
            status=status,
            added=pf.added or 0,
            removed=pf.removed or 0,
            blocks=blocks,
        )
        if not fd.blocks:
            # binary, mode-only, or pure rename: synthesize one whole-file item
            if fd.status in ("modified",):
                fd.status = "renamed" if pf.is_rename else "meta"
            fd.blocks = [Block(runs=[], hunk_id=WHOLE_FILE_ID)]
        changed = [text for b in fd.blocks for r in b.runs for _, text in r.lines]
        if changed and all(t.startswith(_SUBPROJECT) for t in changed):
            fd.status = "submodule"
        files.append(fd)
    return files


def _pointer_shas(fd: FileDiff) -> tuple[str | None, str | None]:
    old = new = None
    for b in fd.blocks:
        for r in b.runs:
            for t, text in r.lines:
                sha = text[len(_SUBPROJECT):].strip().rstrip("-dirty")
                if t == "-":
                    old = sha
                else:
                    new = sha
    return old, new


def expand_submodules(repo: str, files: list[FileDiff]) -> list[FileDiff]:
    """After a submodule pointer item, splice in the submodule's inner diff.

    Best-effort: if the submodule isn't initialized or either commit isn't
    present locally, the pointer item alone remains (still reviewable).
    Depth 1 — nested submodules show as pointer items inside the inner diff.
    """
    import os
    out: list[FileDiff] = []
    for fd in files:
        out.append(fd)
        if fd.status != "submodule":
            continue
        old, new = _pointer_shas(fd)
        sub_repo = os.path.join(repo, fd.path)
        if not (old and new) or not os.path.isdir(sub_repo):
            continue
        try:
            inner = identity_diff(sub_repo, old, rev=new)
        except GitError:
            continue
        for ifd in inner:
            ifd.sub = SubInfo(repo=sub_repo, old=old, new=new, rel=ifd.path,
                              old_rel=ifd.old_path)
            ifd.path = f"{fd.path}/{ifd.path}"
            if ifd.old_path:
                ifd.old_path = f"{fd.path}/{ifd.old_path}"
            out.append(ifd)
    return out


# ---------------------------------------------------------- worktree files

WORKTREE_MAX_BYTES = 5_000_000


def worktree_rows(repo: str, path: str) -> list[Row]:
    """A working-tree file as all-context rows: old == new == i + 1, no hunks.

    Serves walkthrough refs into files that aren't in the diff. Guards mirror
    highlight.guard in spirit: binary and oversized content are hard errors —
    the walkthrough validator already rejected such refs, so hitting one here
    means the file changed underneath us.
    """
    import os
    full = os.path.join(repo, path)
    try:
        with open(full, "rb") as f:
            raw = f.read(WORKTREE_MAX_BYTES + 1)
    except OSError as e:
        raise GitError(f"cannot read {path}: {e}")
    if len(raw) > WORKTREE_MAX_BYTES:
        raise GitError(f"{path} too large ({len(raw) // 1024} kB)")
    if b"\0" in raw[:8192]:
        raise GitError(f"{path} is binary")
    lines = raw.decode("utf-8", "replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [Row(i + 1, i + 1, "context", line) for i, line in enumerate(lines)]


# -------------------------------------------------------------- display pass

def _rows_from_patched_file(pf) -> list[Row]:
    rows: list[Row] = []
    for h in pf:
        for l in h:
            if l.line_type == " ":
                rows.append(Row(l.source_line_no, l.target_line_no, "context",
                                l.value.rstrip("\n")))
            elif l.line_type == "+":
                rows.append(Row(None, l.target_line_no, "add", l.value.rstrip("\n")))
            elif l.line_type == "-":
                rows.append(Row(l.source_line_no, None, "del", l.value.rstrip("\n")))
    return rows


def _tag_rows(rows: list[Row], blocks: list[Block]) -> None:
    """Attach hunk_ids to changed rows by exact line-number lookup."""
    by_new: dict[int, str] = {}
    by_old: dict[int, str] = {}
    for b in blocks:
        for r in b.runs:
            for n in range(r.tgt_start, r.tgt_start + r.tgt_len):
                by_new[n] = b.hunk_id
            for n in range(r.src_start, r.src_start + r.src_len):
                by_old[n] = b.hunk_id
    for row in rows:
        if row.type == "add":
            row.hunk_id = by_new.get(row.new)
        elif row.type == "del":
            row.hunk_id = by_old.get(row.old)


def verify_tagging(rows: list[Row], blocks: list[Block], path: str) -> None:
    """Milestone-3 invariant: every changed row covered by exactly one block."""
    untagged = [r for r in rows if r.type != "context" and r.hunk_id is None]
    if untagged:
        r = untagged[0]
        raise AssertionError(
            f"{path}: {len(untagged)} changed row(s) not covered by any block, "
            f"first at old={r.old} new={r.new} {r.type!r}")
    tagged_ids = {r.hunk_id for r in rows if r.hunk_id}
    empty = [b.hunk_id for b in blocks if b.runs and b.hunk_id not in tagged_ids]
    if empty:
        raise AssertionError(f"{path}: block(s) {empty} matched no rows")


def display_rows(repo: str, mb_sha: str, fd: FileDiff) -> list[Row]:
    """Full-context rows for one file, tagged with block IDs."""
    # submodule inner files diff inside the submodule repo between the pointers
    git_repo = fd.sub.repo if fd.sub else repo
    base = fd.sub.old if fd.sub else mb_sha
    rev = fd.sub.new if fd.sub else "HEAD"
    rel = fd.sub.rel if fd.sub else fd.path
    old_rel = fd.sub.old_rel if fd.sub else fd.old_path

    if fd.status == "deleted":
        # new side is empty; render the old file as deletions
        text = show_file(git_repo, base, rel)
        rows = [Row(i + 1, None, "del", line)
                for i, line in enumerate(text.split("\n"))]
        if rows and rows[-1].text == "":
            rows.pop()
    elif fd.has_content and any(b.runs for b in fd.blocks):
        # both old and new path in the pathspec, or rename detection degrades
        pathspec = [p for p in (old_rel, rel) if p]
        out = _git(git_repo, "diff", "--no-ext-diff", "--no-color", "--unified=999999",
                   "--find-renames", base, rev, "--", *dict.fromkeys(pathspec))
        patch = PatchSet(out)
        if not patch:
            raise GitError(f"no diff output for {fd.path}")
        rows = _rows_from_patched_file(patch[0])
    else:
        # pure rename / mode-only / whole-file item: full content as context
        try:
            text = show_file(git_repo, rev, rel)
        except GitError:
            return []
        rows = [Row(i + 1, i + 1, "context", line)
                for i, line in enumerate(text.split("\n"))]
        if rows and rows[-1].text == "":
            rows.pop()
        return rows

    _tag_rows(rows, fd.blocks)
    verify_tagging(rows, fd.blocks, fd.path)
    return rows


if __name__ == "__main__":
    import sys
    root = repo_root(sys.argv[1] if len(sys.argv) > 1 else ".")
    base = resolve_base(root)
    mb = merge_base(root, base)
    print(f"repo={root} base={base} merge_base={mb[:12]} head={rev_sha(root)[:12]}")
    for fd in identity_diff(root, mb):
        print(f"\n{fd.status:9} {fd.path}  +{fd.added} -{fd.removed}"
              + (f"  (from {fd.old_path})" if fd.old_path else ""))
        for b in fd.blocks:
            if not b.runs:
                print(f"  {b.hunk_id:18} (whole file)")
            else:
                print(f"  {b.hunk_id:18} new {b.new_start}-{b.new_end} "
                      f"+{b.added} -{b.removed} {b.kind}")
