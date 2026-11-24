"""Git integration: ref resolution, diff invocation, parsing, block grouping.

One diff invocation (see PLAN.md §4): `-U0` over the whole diff, once at
startup, for block identity and counts. Display rows are synthesized from those
same runs plus the new file's content — never from a second `git diff`, because
git's edit script is not stable across `-U` widths (a line identical on both
sides can be context under `-U0` and a delete/add pair under full context), and
two scripts that disagree leave rows no block can cover.

Blocks are defined here, not by git: a block is one or more -U0 hunks ("runs")
merged when separated by <= MERGE_GAP unchanged lines. Identity is a content
hash of the changed lines, position-independent.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from unidiff import PatchSet

MERGE_GAP = 3

# A block that covers a whole rewritten file is useless to review: it is one
# checkbox for two thousand lines. So an oversized block is cut into sections
# of between SECTION_MIN and SECTION_MAX changed lines, preferring a cut where
# the code itself has a seam (a definition, an unindented line, a blank line,
# or the -/+ turn). Both bounds are parameters, not a target plus a window, so
# "sections are 20 to 60 lines" says exactly what it means.
SECTION_MIN = 12
SECTION_MAX = 48
BASE_CANDIDATES = ["origin/master", "origin/main", "master", "main"]
WHOLE_FILE_ID = "*"

# A diff side is a commit-ish, or one of these two. They never reach git as a
# revision: every reader branches on them first.
INDEX = "@index"
WORKTREE = "@worktree"
# what HEAD diffs as before the first commit
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# What a run compares. "all" is the branch *including* uncommitted work, which
# is what a reviewer usually means by "my changes"; the other three are its
# layers: committed (merge base → HEAD), staged (HEAD → index) and unstaged
# (index → working tree, untracked files included).
MODES = ("all", "committed", "staged", "unstaged")
DEFAULT_MODE = "all"


def mode_sides(mode: str, mb_sha: str, head_sha: str) -> tuple[str, str]:
    return {
        "all": (mb_sha, WORKTREE),
        "committed": (mb_sha, head_sha),
        "staged": (head_sha, INDEX),
        "unstaged": (INDEX, WORKTREE),
    }[mode]


class GitError(RuntimeError):
    pass


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    # The UI polls git every couple of seconds while the user runs their own
    # git commands: never take index.lock for an opportunistic stat refresh.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    if extra:
        env.update(extra)
    return env


def _git(repo: str, *args: str, env: dict[str, str] | None = None,
         input: str | None = None) -> str:
    # Explicit UTF-8, and never raise on a stray byte: git's output can carry
    # anything a file or a branch name contains, and one undecodable byte must
    # not take a request down. (text=True would use the locale encoding, which
    # is cp1252 on Windows.)
    proc = subprocess.run(
        ["git", "-C", repo, "-c", "core.quotePath=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_env(env), input=input,
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


def head_or_empty(repo: str) -> str:
    """HEAD's sha, or the empty tree on a branch with no commits yet."""
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").strip()
    except GitError:
        return EMPTY_TREE


def commit_date(repo: str, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%cI", sha).strip()


def branch_name(repo: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", repo, "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


@dataclass
class WorkStatus:
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)     # tracked files only
    untracked: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)

    @property
    def dirty(self) -> list[str]:
        return sorted(set(self.staged) | set(self.unstaged) | set(self.untracked))


def work_status(repo: str) -> WorkStatus:
    """One `git status` pass, split the way the diff modes are."""
    out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    st = WorkStatus()
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in "RC":
            i += 1                      # -z puts the rename source next
        if xy == "??":
            if not COMMIT_MSG_RE.fullmatch(path):     # a proposal, not the change
                st.untracked.append(path)
        elif "U" in xy or xy in ("AA", "DD"):
            st.conflicted.append(path)
        else:
            if xy[0] not in " ?!":
                st.staged.append(path)
            if xy[1] != " ":
                st.unstaged.append(path)
    return st


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


def show_side(repo: str, side: str, path: str) -> str:
    """A file's text on one side of a diff: a commit, the index, or disk."""
    if side == WORKTREE:
        # universal newlines, like git output read in text mode above, so
        # the replayed context and the -U0 runs agree on CRLF files
        try:
            with open(os.path.join(repo, path), encoding="utf-8",
                      errors="replace", newline=None) as f:
                return f.read()
        except OSError as e:
            raise GitError(f"cannot read {path}: {e}")
    if side == INDEX:
        return _git(repo, "show", f":{path}")
    return show_file(repo, side, path)


def show_side_bytes(repo: str, side: str, path: str, limit: int) -> bytes:
    """A file's raw bytes on one side of a diff (images): no decoding."""
    if side == WORKTREE:
        try:
            with open(os.path.join(repo, path), "rb") as f:
                data = f.read(limit + 1)
        except OSError as e:
            raise GitError(f"cannot read {path}: {e}")
    else:
        spec = f":{path}" if side == INDEX else f"{side}:{path}"
        proc = subprocess.run(["git", "-C", repo, "show", spec],
                              capture_output=True, env=_env())
        if proc.returncode != 0:
            raise GitError(f"git show {spec} failed: "
                           + proc.stderr.decode("utf-8", "replace").strip())
        data = proc.stdout
    if len(data) > limit:
        raise GitError(f"{path} is larger than {limit // 1_000_000} MB")
    return data


# Shown as pictures instead of rows. SVG is left out on purpose: git diffs it
# as text, and its source is what a reviewer needs to read.
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".avif": "image/avif",
}


def image_type(path: str) -> str | None:
    return IMAGE_TYPES.get(os.path.splitext(path)[1].lower())


def diff_sides(fd: "FileDiff", repo: str) -> dict[str, tuple[str, str, str] | None]:
    """{"old"|"new": (repo, side, path) or None} — where each side's bytes live,
    submodule inner files included."""
    if fd.sub:
        r, old, new = fd.sub.repo, fd.sub.old, fd.sub.new
        old_path, new_path = fd.sub.old_rel or fd.sub.rel, fd.sub.rel
    else:
        r, old, new = repo, fd.old_side, fd.new_side
        old_path, new_path = fd.old_path or fd.path, fd.path
    return {"old": (r, old, old_path) if fd.has_old else None,
            "new": (r, new, new_path) if fd.has_new else None}


@contextlib.contextmanager
def _untracked_index(repo: str):
    """Env for a git call that should see untracked files as new files.

    `git diff` only knows paths in the index. Rather than synthesize patches
    for untracked files (and get binary detection, newline markers and hashes
    subtly different from what git emits once the file is added), copy the
    index aside and mark them intent-to-add there. The user's real index is
    never touched. Yields None when there is nothing untracked.
    """
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard",
                     f"--exclude={COMMIT_MSG_GLOB}", "-z")
    if not untracked.strip("\0"):
        yield None
        return
    real = _git(repo, "rev-parse", "--git-path", "index").strip()
    real = os.path.join(repo, real)
    tmpdir = tempfile.mkdtemp(prefix="seendiff-idx-")
    try:
        tmp = os.path.join(tmpdir, "index")
        if os.path.exists(real):
            # copy2, not copyfile: the copy must keep the index's mtime. Git
            # re-reads a file's content when its mtime is not older than the
            # index file ("racy git"); a fresh mtime on the copy turns that
            # off, and a same-size edit made within one timestamp tick of the
            # last `git add` would then be reported as unchanged.
            shutil.copy2(real, tmp)
        env = {"GIT_INDEX_FILE": tmp}
        _git(repo, "add", "--intent-to-add", "--pathspec-from-file=-",
             "--pathspec-file-nul", env=env, input=untracked)
        yield env
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def raw_diff(repo: str, old: str, new: str, untracked: bool = True) -> str:
    """The -U0 patch text between two sides (see INDEX / WORKTREE).

    --full-index puts real blob ids in every header — git hashes working-tree
    content for it too — so one file's patch text identifies both of its
    sides exactly, which is what the payload cache keys on.
    """
    args = ["diff", "--no-ext-diff", "--no-color", "--unified=0",
            "--find-renames", "--full-index"]
    if new in (INDEX, WORKTREE):
        # Mid-merge, an unmerged path diffs as a combined (--cc) patch, which
        # neither unidiff nor this viewer models. seendiff is not a merge
        # tool: leave them out, the UI lists them as conflicted.
        # (--diff-filter=u does not do it: the worktree side isn't unmerged.)
        unmerged = sorted({line.split("\t", 1)[1] for line in
                           _git(repo, "ls-files", "-u", "-z").split("\0")
                           if "\t" in line})
        spec = ["--", *(f":(exclude,literal){p}" for p in unmerged)]
    else:
        spec = ["--"]
    if new == WORKTREE:
        if old != INDEX:
            args.append(old)
        if not untracked:
            return _git(repo, *args, *spec)
        with _untracked_index(repo) as env:
            return _git(repo, *args, *spec, env=env)
    if new == INDEX:
        return _git(repo, *args, "--cached", old, *spec)
    return _git(repo, *args, old, new, *spec)


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
    # the -U0 runs as git emitted them, before split_blocks cut any of them:
    # display rows are replayed from these, and blocks tile them exactly
    runs: list[Run] = field(default_factory=list)
    # what was compared: commit-ish, INDEX or WORKTREE
    old_side: str = ""
    new_side: str = "HEAD"
    # hash of this file's patch text: changes iff either side's content does
    fingerprint: str = ""
    # which sides exist: an added file has no old one, a deleted no new one.
    # status can't carry this for binaries, which are all just "binary".
    has_old: bool = True
    has_new: bool = True

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


# Language-agnostic on purpose: no parser, no per-language table. A cut is
# preferred where a line looks like the start of a definition *and* sits at
# indent 0 — the two heuristics reinforce each other, and a file with neither
# (JSON, minified JS) simply falls back to the size bound.
_DEF_RE = re.compile(
    r"^(@\w|#\s*(define|include|pragma)\b|"
    r"(export|default|public|private|protected|internal|static|final|"
    r"abstract|pub|async|inline|extern)\s+)*"
    r"(def|class|fn|func|function|impl|struct|enum|trait|interface|"
    r"namespace|module|mod|package|type|template|const|let|var)\b"
)


def _seam_score(lines: list[tuple[str, str]], i: int) -> int:
    """How good a cut between lines[i-1] and lines[i] is. 0 = arbitrary."""
    prev_type, prev_text = lines[i - 1]
    cur_type, cur_text = lines[i]
    score = 0
    if cur_type != prev_type:
        score += 4                      # the -/+ turn: removals, then their replacement
    if not prev_text.strip():
        score += 2                      # a blank line already separates these
    body = cur_text.rstrip("\r\n")
    stripped = body.lstrip()
    if stripped:
        if len(body) - len(stripped) == 0:
            score += 2                  # top level
        if _DEF_RE.match(stripped):
            score += 3                  # ...and it declares something
    return score


def _cut_points(lines: list[tuple[str, str]], lo: int, hi: int) -> list[int]:
    """Indices to cut a run at, each section between lo and hi lines long."""
    cuts: list[int] = []
    start, n = 0, len(lines)
    while n - start > hi:
        lower = start + lo
        upper = min(start + hi, n - lo)   # leave the tail at least lo lines
        if upper < lower:
            upper = min(start + hi, n - 1)
        if upper < lower:
            break
        # best seam wins; ties go to the longer section, so we cut less often
        cut = max(range(lower, upper + 1), key=lambda i: (_seam_score(lines, i), i))
        cuts.append(cut)
        start = cut
    return cuts


def _split_run(run: Run, lo: int, hi: int) -> list[Run]:
    """One oversized run into several, line numbers recomputed per section."""
    cuts = _cut_points(run.lines, lo, hi)
    if not cuts:
        return [run]
    subs: list[Run] = []
    bounds = [0, *cuts, len(run.lines)]
    dels = adds = 0
    for lo_i, hi_i in zip(bounds, bounds[1:]):
        seg = run.lines[lo_i:hi_i]
        nd = sum(1 for t, _ in seg if t == "-")
        na = len(seg) - nd
        # a zero-length side keeps git's "anchor at the line before" convention
        # loosely: nothing reads it, since _tag_rows iterates an empty range.
        subs.append(Run(src_start=run.src_start + dels, src_len=nd,
                        tgt_start=run.tgt_start + adds, tgt_len=na, lines=seg))
        dels += nd
        adds += na
    return subs


def _changed(runs: list[Run]) -> int:
    return sum(len(r.lines) for r in runs)


def split_blocks(blocks: list[Block], lo: int = SECTION_MIN,
                 hi: int = SECTION_MAX) -> list[Block]:
    """Cut blocks larger than hi into sections. Run boundaries first: a block
    that is several runs merged across small gaps splits between them, which
    is where the edit itself paused. Only a single run larger than hi is cut
    internally, at the best seam its lines offer."""
    out: list[Block] = []
    for b in blocks:
        if _changed(b.runs) <= hi:
            out.append(b)
            continue
        group: list[Run] = []
        for run in b.runs:
            if len(run.lines) > hi:
                if group:
                    out.append(Block(runs=group))
                    group = []
                out.extend(Block(runs=[sub]) for sub in _split_run(run, lo, hi))
                continue
            if group and _changed(group) + len(run.lines) > hi:
                out.append(Block(runs=group))
                group = []
            group.append(run)
        if group:
            out.append(Block(runs=group))
    return out


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


def identity_diff(repo: str, mb_sha: str, rev: str = "HEAD",
                  section_min: int = SECTION_MIN,
                  section_max: int = SECTION_MAX,
                  untracked: bool = True) -> list[FileDiff]:
    """The -U0 pass: every changed file with grouped, hashed blocks.

    `mb_sha` and `rev` are diff sides: commit-ishes, INDEX or WORKTREE.
    """
    return parse_diff(raw_diff(repo, mb_sha, rev, untracked), mb_sha, rev,
                      section_min, section_max)


def parse_diff(out: str, old: str, new: str,
               section_min: int = SECTION_MIN,
               section_max: int = SECTION_MAX) -> list[FileDiff]:
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
        blocks = split_blocks(_group_blocks(runs), section_min, section_max)
        _assign_ids(blocks)
        fd = FileDiff(
            runs=runs,
            path=_strip_ab(pf.path),
            old_path=_strip_ab(pf.source_file) if pf.is_rename else None,
            status=status,
            added=pf.added or 0,
            removed=pf.removed or 0,
            blocks=blocks,
            old_side=old, new_side=new,
            has_old=not pf.is_added_file, has_new=not pf.is_removed_file,
            fingerprint=hashlib.sha256(
                str(pf).encode("utf-8", "replace")).hexdigest()[:16],
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
                # removesuffix, not rstrip: rstrip("-dirty") eats any trailing
                # 'd', 'i', 'r', 't' or 'y', and a hex sha can end in 'd'
                sha = text[len(_SUBPROJECT):].strip().removesuffix("-dirty")
                if t == "-":
                    old = sha
                else:
                    new = sha
    return old, new


def expand_submodules(repo: str, files: list[FileDiff],
                      section_min: int = SECTION_MIN,
                      section_max: int = SECTION_MAX) -> list[FileDiff]:
    """After a submodule pointer item, splice in the submodule's inner diff.

    Best-effort: if the submodule isn't initialized or either commit isn't
    present locally, the pointer item alone remains (still reviewable).
    Depth 1 — nested submodules show as pointer items inside the inner diff.
    """
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
            inner = identity_diff(sub_repo, old, rev=new,
                                  section_min=section_min,
                                  section_max=section_max)
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


# ------------------------------------------------------------- committing
#
# Proposed commit messages are plain files in the repo root, one per queued
# change: COMMIT_MSG_<description>.txt. Several can wait at once (one for what
# is staged, one for the next change, one per agent session), and an IDE sees
# them as ordinary files. Committing takes what is staged plus one of them.

COMMIT_MSG_RE = re.compile(r"COMMIT_MSG(?:[_.-][\w.-]*)?\.txt")
COMMIT_MSG_GLOB = "/COMMIT_MSG*.txt"      # root only, like the regex
COMMIT_MSG_MAX = 64_000


def commit_msg_description(name: str) -> str:
    d = name[len("COMMIT_MSG"):-len(".txt")].lstrip("_.-")
    return d.replace("_", " ").replace("-", " ").strip() or "(untitled)"


def commit_messages(repo: str) -> list[dict]:
    """The proposed messages waiting in the repo root, oldest first."""
    out = []
    try:
        names = os.listdir(repo)
    except OSError:
        return []
    for name in names:
        if not COMMIT_MSG_RE.fullmatch(name):
            continue
        full = os.path.join(repo, name)
        try:
            st = os.stat(full)
            if not os.path.isfile(full) or st.st_size > COMMIT_MSG_MAX:
                continue
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        out.append({"name": name, "description": commit_msg_description(name),
                    # a string: nanoseconds since 1970 exceed 2**53, and a
                    # JS number would round them, so no stamp would match
                    "text": text, "mtime_ns": str(st.st_mtime_ns)})
    out.sort(key=lambda m: (int(m["mtime_ns"]), m["name"]))
    return out


def staged_changes(repo: str) -> list[dict]:
    """What a commit would take: `git diff --cached --name-status`."""
    out = _git(repo, "diff", "--cached", "--name-status", "-z", "--find-renames")
    parts = out.split("\0")
    res, i = [], 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        if status[:1] in "RC":
            res.append({"status": status[0], "path": parts[i + 2],
                        "old_path": parts[i + 1]})
            i += 3
        else:
            res.append({"status": status[0], "path": parts[i + 1]})
            i += 2
    return res


def commit(repo: str, message: str) -> str:
    """Commit what is staged with this message; returns "<sha> <subject>".

    --cleanup=strip drops '#' lines and surrounding blank lines, the same as
    committing from the editor, so a proposal can carry notes to the reviewer.
    Hooks run as usual; their complaint comes back as the GitError text.
    """
    _git(repo, "commit", "--cleanup=strip", "-F", "-", input=message)
    return _git(repo, "log", "-1", "--format=%h %s").strip()


# ---------------------------------------------------------------- pushing

PUSH_TIMEOUT = 180


def push_status(repo: str) -> dict:
    """What `push` would do: where to, how many commits, or why it can't.

    Counts come from the local remote-tracking refs, so they are as fresh as
    the last fetch. A branch behind its upstream is blocked rather than left
    to git to reject: seendiff never force-pushes, so the answer is a pull.
    """
    out = {"branch": branch_name(repo), "upstream": None, "remote": None,
           "ahead": 0, "behind": 0, "commits": [], "blocked": None}
    if not out["branch"]:
        out["blocked"] = "detached HEAD: check out a branch to push"
        return out
    try:
        up = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                  "@{upstream}").strip()
    except GitError:
        up = None
    try:
        if up:
            out["upstream"] = up
            behind, ahead = _git(repo, "rev-list", "--left-right", "--count",
                                 "@{upstream}...HEAD").split()
            out["behind"], out["ahead"] = int(behind), int(ahead)
            rng = ["@{upstream}..HEAD"]
        else:
            remotes = _git(repo, "remote").split()
            remote = ("origin" if "origin" in remotes
                      else remotes[0] if len(remotes) == 1 else None)
            if remote is None:
                out["blocked"] = ("no remote to push to" if not remotes else
                                  "no upstream and several remotes: "
                                  "set one with git push -u <remote>")
                return out
            out["remote"] = remote
            # commits no remote has yet: what a first push would publish
            out["ahead"] = int(_git(repo, "rev-list", "--count", "HEAD",
                                    "--not", "--remotes").strip())
            rng = ["HEAD", "--not", "--remotes"]
        if out["ahead"]:
            log = _git(repo, "log", "--format=%h%x00%s", "-n", "20", *rng)
            out["commits"] = [dict(zip(("sha", "subject"), line.split("\0", 1)))
                              for line in log.splitlines() if "\0" in line]
    except GitError:
        out["blocked"] = "nothing committed on this branch yet"
        return out
    if out["behind"]:
        n = out["behind"]
        out["blocked"] = (f"{up} has {n} commit{'s' if n > 1 else ''} this "
                          "branch doesn't: pull first")
    return out


def push(repo: str) -> str:
    """Push the current branch; returns git's report. Never forces.

    No terminal is attached (new session, stdin closed, prompts off): a
    credential prompt would otherwise hang the request, or appear in whatever
    terminal started seendiff. An SSH agent, a credential helper or a desktop
    askpass dialog all still work; anything else fails with git's message.
    """
    st = push_status(repo)
    if st["blocked"]:
        raise GitError(st["blocked"])
    if not st["ahead"]:
        raise GitError("nothing to push")
    args = (["push"] if st["upstream"] else
            ["push", "--set-upstream", st["remote"], st["branch"]])
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
            env=_env({"GIT_TERMINAL_PROMPT": "0"}), start_new_session=True,
            timeout=PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GitError(f"git push gave up after {PUSH_TIMEOUT} s")
    report = (proc.stderr + proc.stdout).strip()
    if proc.returncode != 0:
        raise GitError(report or f"git push failed ({proc.returncode})")
    return report


# ---------------------------------------------------------- worktree files

WORKTREE_MAX_BYTES = 5_000_000


def worktree_rows(repo: str, path: str) -> list[Row]:
    """A working-tree file as all-context rows: old == new == i + 1, no hunks.

    Serves walkthrough refs into files that aren't in the diff. Guards mirror
    highlight.guard in spirit: binary and oversized content are hard errors —
    the walkthrough validator already rejected such refs, so hitting one here
    means the file changed underneath us.
    """
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

def _split_text(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _rows_from_runs(runs: list[Run], new_text: str, path: str) -> list[Row]:
    """Replay the -U0 runs against the new file to get full-context rows.

    Everything between two runs is unchanged by definition, so it can be read
    straight off the new side; deleted text only exists in the runs, which is
    why they carry their lines. Doing it this way — rather than asking git for
    the same diff again at full context — is what guarantees every changed row
    falls inside a block: rows and blocks come from one edit script.
    """
    new_lines = _split_text(new_text)
    rows: list[Row] = []
    old_i = new_i = 1
    for r in runs:
        # git anchors a zero-length side *after* the line it names, so the last
        # unchanged line before the run is `start` there and `start - 1` otherwise
        ctx_end = r.tgt_start - 1 if r.tgt_len else r.tgt_start
        if ctx_end > len(new_lines) or ctx_end < new_i - 1:
            raise GitError(f"{path}: hunk at new line {r.tgt_start} does not fit "
                           f"the file ({len(new_lines)} lines) — it changed under us")
        while new_i <= ctx_end:
            rows.append(Row(old_i, new_i, "context", new_lines[new_i - 1]))
            old_i += 1
            new_i += 1
        dels = [t for k, t in r.lines if k == "-"]
        adds = [t for k, t in r.lines if k == "+"]
        rows += [Row(r.src_start + i, None, "del", t.rstrip("\n"))
                 for i, t in enumerate(dels)]
        rows += [Row(None, r.tgt_start + i, "add", t.rstrip("\n"))
                 for i, t in enumerate(adds)]
        if r.src_len:
            old_i = r.src_start + r.src_len
        if r.tgt_len:
            new_i = r.tgt_start + r.tgt_len
    while new_i <= len(new_lines):
        rows.append(Row(old_i, new_i, "context", new_lines[new_i - 1]))
        old_i += 1
        new_i += 1
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
    base = fd.sub.old if fd.sub else (fd.old_side or mb_sha)
    rev = fd.sub.new if fd.sub else fd.new_side
    rel = fd.sub.rel if fd.sub else fd.path

    if fd.status == "binary" or (not fd.runs and image_type(fd.path)):
        # no lines to show; decoding a PNG as text only produces noise (images,
        # renamed ones included, get their own view from the raw bytes)
        return []
    if fd.status == "submodule":
        # a gitlink has no blob to read: the pointer lines are the whole file
        rows = _rows_from_runs(fd.runs, "", fd.path)
    elif fd.status == "deleted":
        # new side is empty; render the old file as deletions
        old_rel = (fd.sub.old_rel if fd.sub else fd.old_path) or rel
        rows = [Row(i + 1, None, "del", line)
                for i, line in enumerate(_split_text(show_side(git_repo, base, old_rel)))]
    elif fd.has_content and fd.runs:
        rows = _rows_from_runs(fd.runs, show_side(git_repo, rev, rel), fd.path)
    else:
        # pure rename / mode-only / whole-file item: full content as context
        try:
            text = show_side(git_repo, rev, rel)
        except GitError:
            return []
        return [Row(i + 1, i + 1, "context", line)
                for i, line in enumerate(_split_text(text))]

    _tag_rows(rows, fd.blocks)
    try:
        verify_tagging(rows, fd.blocks, fd.path)
    except AssertionError as e:
        # An invariant break is a bug in here, not in the user's repo, and it
        # must not cost them the file: untagged rows still read, they just
        # can't be marked reviewed. Loud on the console, survivable in the UI.
        print(f"seendiff: tagging invariant broken, showing {fd.path} "
              f"without block marks — please report: {e}", file=sys.stderr)
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
