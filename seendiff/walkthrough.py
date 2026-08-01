"""Walkthrough loading, validation and ref resolution (CODEWALKTHROUGH.md §2).

Pure: no FastAPI, no globals. The input is machine-generated, so validation
fails loudly and completely — every problem is collected and reported, never
silently skipped. A walkthrough that doesn't fully resolve never loads.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_STEPS = 40
MAX_ALSO = 3
MAX_DEPTH = 1


class WalkthroughError(ValueError):
    """Raised by load(); .problems carries every diagnostic."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Ref:
    path: str
    lines: tuple[int, int]
    hunk_id: str | None = None
    note: str | None = None

    def to_json(self) -> dict:
        d: dict = {"path": self.path, "lines": list(self.lines)}
        if self.hunk_id:
            d["hunk_id"] = self.hunk_id
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class Step:
    id: str
    act: str
    depth: int
    title: str
    text: str
    ref: Ref
    also: list[Ref] = field(default_factory=list)
    forward: str | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id, "act": self.act, "depth": self.depth,
            "title": self.title, "text": self.text,
            "ref": self.ref.to_json(),
            "also": [r.to_json() for r in self.also],
            "forward": self.forward,
        }


@dataclass
class Act:
    id: str
    title: str


@dataclass
class Walkthrough:
    title: str
    intro: str
    acts: list[Act]
    steps: list[Step]
    path: str
    mtime: float
    digest: str

    def paths(self) -> set[str]:
        out = set()
        for s in self.steps:
            out.add(s.ref.path)
            out.update(r.path for r in s.also)
        return out

    def to_json(self) -> dict:
        return {
            "title": self.title, "intro": self.intro,
            "acts": [{"id": a.id, "title": a.title} for a in self.acts],
            "steps": [s.to_json() for s in self.steps],
            "digest": self.digest, "mtime": self.mtime,
        }


# ------------------------------------------------------------------ checking

def file_line_count(repo: str, path: str) -> int | None:
    """Line count of a worktree file, or None if unreadable/binary."""
    try:
        raw = (Path(repo) / path).read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    lines = raw.decode("utf-8", "replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return len(lines)


def _check_ref(ref: dict, repo: str, where: str,
               hunk_ids: dict[str, set[str]] | None) -> tuple[Ref | None, list[str]]:
    probs: list[str] = []
    path = ref.get("path")
    if not isinstance(path, str) or not path:
        return None, [f"{where}: ref needs a string 'path'"]
    # file-read primitive reachable from a JSON file: containment is load-bearing
    root = Path(repo).resolve()
    try:
        resolved = (root / path).resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return None, [f"{where}: {path} resolves outside the repository"]
    if not resolved.is_file():
        return None, [f"{where}: {path} does not exist"]

    lines = ref.get("lines")
    if (not isinstance(lines, (list, tuple)) or len(lines) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in lines)):
        return None, [f"{where}: 'lines' must be [start, end] integers"]
    a, b = lines
    n = file_line_count(repo, path)
    if n is None:
        probs.append(f"{where}: {path} is binary or unreadable")
    elif not (1 <= a <= b <= n):
        probs.append(f"{where}: {path}:{a}-{b} out of range "
                     f"(file has {n} lines)" if a <= b else
                     f"{where}: {path}:{a}-{b} is inverted")

    hunk_id = ref.get("hunk_id")
    if hunk_id is not None:
        if not isinstance(hunk_id, str):
            probs.append(f"{where}: 'hunk_id' must be a string")
        elif hunk_ids is not None and hunk_id not in hunk_ids.get(path, set()):
            probs.append(f"{where}: hunk_id {hunk_id!r} not in the diff of {path}")

    note = ref.get("note")
    if note is not None and not isinstance(note, str):
        probs.append(f"{where}: 'note' must be a string")
    if probs:
        return None, probs
    return Ref(path=path, lines=(a, b), hunk_id=hunk_id, note=note), []


def _parse(data: object, repo: str,
           hunk_ids: dict[str, set[str]] | None) -> tuple[Walkthrough | None, list[str]]:
    probs: list[str] = []
    if not isinstance(data, dict):
        return None, ["top level must be a JSON object"]
    if data.get("version") != 1:
        probs.append(f"version must be 1 (got {data.get('version')!r})")
    title = data.get("title")
    if not isinstance(title, str) or not title:
        probs.append("'title' is required")
        title = ""
    intro = data.get("intro") or ""

    acts: list[Act] = []
    act_ids: dict[str, int] = {}
    raw_acts = data.get("acts")
    if not isinstance(raw_acts, list) or not raw_acts:
        probs.append("'acts' must be a non-empty list")
        raw_acts = []
    for i, a in enumerate(raw_acts):
        if (not isinstance(a, dict) or not isinstance(a.get("id"), str)
                or not isinstance(a.get("title"), str)):
            probs.append(f"acts[{i}]: needs string 'id' and 'title'")
            continue
        if a["id"] in act_ids:
            probs.append(f"acts[{i}]: duplicate act id {a['id']!r}")
            continue
        act_ids[a["id"]] = len(acts)
        acts.append(Act(id=a["id"], title=a["title"]))

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        probs.append("'steps' must be a non-empty list")
        raw_steps = []
    if len(raw_steps) > MAX_STEPS:
        probs.append(f"{len(raw_steps)} steps — cap is {MAX_STEPS}; "
                     "the scope was too big")

    steps: list[Step] = []
    step_index: dict[str, int] = {}
    last_act = -1
    for i, s in enumerate(raw_steps):
        w = f"steps[{i}]"
        if not isinstance(s, dict):
            probs.append(f"{w}: must be an object")
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            probs.append(f"{w}: needs a string 'id'")
            sid = f"#{i}"
        elif sid in step_index:
            probs.append(f"{w}: duplicate step id {sid!r}")
        act = s.get("act")
        if act not in act_ids:
            probs.append(f"{w}: unknown act {act!r}")
        else:
            # steps must appear in act order, contiguously: the pip strip and
            # the act-jump keys assume it
            ai = act_ids[act]
            if ai < last_act:
                probs.append(f"{w}: act {act!r} interleaved — steps must be "
                             "grouped in act order")
            last_act = max(last_act, ai)
        depth = s.get("depth", 0)
        if not isinstance(depth, int) or not (0 <= depth <= MAX_DEPTH):
            probs.append(f"{w}: depth must be 0 or 1 (got {depth!r}); "
                         "three levels of nesting needs its own act")
            depth = 0
        st_title = s.get("title")
        if not isinstance(st_title, str) or not st_title:
            probs.append(f"{w}: needs a string 'title'")
            st_title = ""
        text = s.get("text")
        if not isinstance(text, str) or not text:
            probs.append(f"{w}: needs a string 'text'")
            text = ""
        if not isinstance(s.get("ref"), dict):
            probs.append(f"{w}: needs a 'ref' object")
            continue
        ref, rp = _check_ref(s["ref"], repo, f"{w}.ref", hunk_ids)
        probs.extend(rp)
        also: list[Ref] = []
        raw_also = s.get("also") or []
        if not isinstance(raw_also, list):
            probs.append(f"{w}: 'also' must be a list")
            raw_also = []
        if len(raw_also) > MAX_ALSO:
            probs.append(f"{w}: {len(raw_also)} also-refs — cap is {MAX_ALSO}")
        for j, ar in enumerate(raw_also):
            aref, ap = _check_ref(ar if isinstance(ar, dict) else {},
                                  repo, f"{w}.also[{j}]", hunk_ids)
            probs.extend(ap)
            if aref:
                also.append(aref)
        forward = s.get("forward")
        if forward is not None and not isinstance(forward, str):
            probs.append(f"{w}: 'forward' must be a step id")
            forward = None
        if ref is None:
            continue
        step_index.setdefault(sid, len(steps))
        steps.append(Step(id=sid, act=act if act in act_ids else "",
                          depth=depth, title=st_title, text=text,
                          ref=ref, also=also, forward=forward))

    # forward refs must exist and point *after* the referring step — this is
    # what makes "more on that later" structural
    for i, st in enumerate(steps):
        if st.forward is None:
            continue
        j = step_index.get(st.forward)
        if j is None:
            probs.append(f"steps[{i}]: forward {st.forward!r} names no step")
        elif j <= i:
            probs.append(f"steps[{i}]: forward {st.forward!r} must point to a "
                         "later step")

    if probs:
        return None, probs
    return Walkthrough(title=title, intro=intro, acts=acts, steps=steps,
                       path="", mtime=0.0, digest=""), []


# ------------------------------------------------------------------- public

def check(path: str, repo: str,
          hunk_ids: dict[str, set[str]] | None = None) -> list[str]:
    """All diagnostics for a walkthrough file, human-readable, empty if OK."""
    name = os.path.basename(path)
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        return [f"{name}: cannot read: {e}"]
    try:
        data = json.loads(raw)
    except ValueError as e:
        return [f"{name}: invalid JSON: {e}"]
    _, probs = _parse(data, repo, hunk_ids)
    return [f"{name}: {p}" for p in probs]


def load(path: str, repo: str,
         hunk_ids: dict[str, set[str]] | None = None) -> Walkthrough:
    """Parse + validate + resolve; raises WalkthroughError with everything."""
    try:
        raw = Path(path).read_bytes()
        mtime = os.stat(path).st_mtime
    except OSError as e:
        raise WalkthroughError([f"cannot read {path}: {e}"])
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise WalkthroughError([f"invalid JSON: {e}"])
    wt, probs = _parse(data, repo, hunk_ids)
    if wt is None:
        raise WalkthroughError(probs)
    wt.path = path
    wt.mtime = mtime
    wt.digest = hashlib.sha256(raw).hexdigest()[:16]
    return wt
