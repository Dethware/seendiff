"""FastAPI app: API routes, caches, theme.css generation (PLAN.md §8)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pygments.formatters import HtmlFormatter

from . import git as G
from . import highlight as H
from . import store
from . import walkthrough as W

STATIC = Path(__file__).parent / "static"

THEME_STYLES = {
    "light": "default",
    "dark": "github-dark",
    "solarized-dark": "solarized-dark",
    "solarized-light": "solarized-light",
    "space": "nord",
    "hailmary": "coffee",
}

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
if os.environ.get("SEENDIFF_ALLOW_HOST"):        # test/dev escape hatch only
    _ALLOWED_HOSTS.add(os.environ["SEENDIFF_ALLOW_HOST"])
CACHE_PRUNE_DAYS = 30
SEARCH_LIMIT = 500       # hits returned by /api/search before it gives up


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return Path(base) / "seendiff"


def prune_cache(root: Path, days: int = CACHE_PRUNE_DAYS) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - days * 86400
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
        except OSError:
            pass


@dataclass
class AppState:
    repo: str
    base_ref: str
    no_highlight: bool = False
    auto_seen: bool = True
    db_path: str | None = None

    mb_sha: str = ""
    head_sha: str = ""
    base_date: str = ""
    branch: str | None = None
    scope: str = ""
    files: list[G.FileDiff] = field(default_factory=list)
    by_path: dict[str, G.FileDiff] = field(default_factory=dict)
    dirty: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)
    file_cache: dict[str, dict] = field(default_factory=dict)

    # walkthrough mode (CODEWALKTHROUGH.md §2)
    wt_file: str | None = None                 # path of the JSON, if any
    wt: W.Walkthrough | None = None            # last good copy
    wt_empty: bool = True                      # file absent/empty: plain diff
    wt_error: str | None = None                # why the latest reload failed
    wt_attempt_mtime: float = -1.0             # last mtime we tried to load
    wt_paths: set[str] = field(default_factory=set)   # wt files not in the diff
    wt_cache: dict[tuple[str, float], dict] = field(default_factory=dict)

    def hunk_ids(self) -> dict[str, set[str]]:
        return {p: {b.hunk_id for b in fd.blocks}
                for p, fd in self.by_path.items()}

    def _recompute_wt_paths(self) -> None:
        self.wt_paths = (self.wt.paths() - set(self.by_path)
                         if self.wt else set())

    def load_walkthrough(self) -> None:
        """(Re)load when the file's mtime moved; keep the last good copy.

        A reload that fails validation leaves self.wt intact and records the
        error — the bar shows it in place of the prose, never a blank bar
        because the agent wrote a half-flushed file.

        A file that is absent or empty is *not* a failure: that's the empty
        walkthrough, seendiff plays plain diff mode and keeps watching, so the
        agent can fill the script in while the tab stays open.
        """
        if not self.wt_file:
            return
        try:
            mtime = os.stat(self.wt_file).st_mtime
        except FileNotFoundError:
            self.wt, self.wt_error, self.wt_empty = None, None, True
            self.wt_attempt_mtime = -1.0     # reload as soon as it appears
            self._recompute_wt_paths()
            return
        except OSError as e:
            self.wt_error = f"cannot stat walkthrough: {e}"
            return
        if mtime == self.wt_attempt_mtime:
            return
        self.wt_attempt_mtime = mtime
        try:
            self.wt = W.load(self.wt_file, self.repo, self.hunk_ids())
            self.wt_empty = self.wt is None
            self.wt_error = None
            self.wt_cache.clear()
            self._recompute_wt_paths()
        except W.WalkthroughError as e:
            self.wt_error = "\n".join(e.problems)

    def refresh(self) -> None:
        with self.lock:
            self.mb_sha = G.merge_base(self.repo, self.base_ref)
            self.head_sha = G.rev_sha(self.repo)
            self.base_date = G.commit_date(self.repo, self.mb_sha)
            self.branch = G.branch_name(self.repo)
            self.scope = store.scope_key(self.repo, self.base_ref, self.branch,
                                         self.mb_sha)
            self.files = G.expand_submodules(
                self.repo, G.identity_diff(self.repo, self.mb_sha))
            self.by_path = {f.path: f for f in self.files}
            self.dirty, self.conflicted = G.dirty_and_conflicted(self.repo)
            self.file_cache.clear()
            self._recompute_wt_paths()

    def maybe_refresh(self) -> None:
        # diff is immutable per (mb, head); recompute only when HEAD moved
        if G.rev_sha(self.repo) != self.head_sha:
            self.refresh()
        else:
            self.dirty, self.conflicted = G.dirty_and_conflicted(self.repo)

    def disk_cache_dir(self) -> Path:
        return cache_root() / f"{self.mb_sha[:12]}-{self.head_sha[:12]}"


def _theme_css() -> str:
    parts = []
    for theme, style in THEME_STYLES.items():
        css = HtmlFormatter(style=style).get_style_defs(
            f'[data-theme="{theme}"] .hl')
        # the app owns backgrounds; a style's own background (nord #2e3440,
        # default #f8f8f8) would otherwise paint a box behind every line
        css = "\n".join(
            line for line in css.splitlines()
            if "background" not in line or " .hl ." in line)
        parts.append(f"/* {theme}: pygments {style} */\n{css}")
    return "\n\n".join(parts)


def _file_states(st: AppState, db) -> dict[tuple[str, str], tuple]:
    return store.get_states(db, st.scope)


def _counts(fd: G.FileDiff, states) -> tuple[int, int, int]:
    seen = reviewed = 0
    for b in fd.blocks:
        s, r = states.get((fd.path, b.hunk_id), (None, None))
        if r:
            reviewed += 1
        elif s:
            seen += 1
    return len(fd.blocks), seen, reviewed


def _build_file_payload(st: AppState, fd: G.FileDiff) -> dict:
    """The immutable part of /api/file (rows + tokens), disk-cached."""
    key = hashlib.sha256(fd.path.encode()).hexdigest()[:16] + ".json"
    disk = st.disk_cache_dir() / key
    if fd.path in st.file_cache:
        return st.file_cache[fd.path]
    if disk.is_file():
        try:
            payload = json.loads(disk.read_text())
            st.file_cache[fd.path] = payload
            return payload
        except (OSError, ValueError):
            pass

    rows = G.display_rows(st.repo, st.mb_sha, fd)
    toks = None if st.no_highlight else H.highlight_rows(rows, fd.path)
    skip_reason = None if st.no_highlight else H.guard(rows)
    lines = []
    for i, r in enumerate(rows):
        line = {"old": r.old, "new": r.new, "type": r.type}
        if r.hunk_id:
            line["h"] = r.hunk_id
        if toks is not None:
            line["t"] = toks[i]
        else:
            line["text"] = r.text
        lines.append(line)
    payload = {
        "path": fd.path,
        "old_path": fd.old_path,
        "status": fd.status,
        "lang": (H.lang_for(fd.path) or "text") if toks is not None else "text",
        "total_lines": len(rows),
        "highlighted": toks is not None,
        "skip_reason": skip_reason,
        "block_meta": [
            {"hunk_id": b.hunk_id,
             "start": b.new_start if b.runs else 1,
             "end": b.new_end if b.runs else max(1, len(rows)),
             "added": b.added, "removed": b.removed,
             "kind": b.kind if b.runs else "meta"}
            for b in fd.blocks
        ],
        "lines": lines,
    }
    st.file_cache[fd.path] = payload
    try:
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_text(json.dumps(payload))
    except OSError:
        pass
    return payload


def line_text(line: dict) -> str:
    """The plain text of a payload line, highlighted or not."""
    toks = line.get("t")
    if toks is not None:
        return "".join(piece[1] for piece in toks)
    return line.get("text") or ""


async def _json_body(request: Request) -> dict:
    # sendBeacon may arrive as text/plain; parse the raw body ourselves
    raw = await request.body()
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        raise HTTPException(400, "invalid JSON body")


def create_app(st: AppState) -> FastAPI:
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    db = store.open_db(st.db_path)
    st.refresh()
    prune_cache(cache_root())
    theme_css = _theme_css()

    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        host = request.headers.get("host", "").rsplit(":", 1)[0]
        if host not in _ALLOWED_HOSTS:
            return Response("forbidden host", status_code=403)
        return await call_next(request)

    @app.get("/")
    def index():
        # prefs are inlined into the document so the first paint already uses
        # the saved theme/font instead of flashing a default and correcting
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = html.replace("__PREFS__", json.dumps(store.get_prefs(db)), 1)
        # The syntax palette is inlined, not linked. As a separate file it was
        # cached for a day, so renaming a theme (which rewrites every selector
        # in it) left browsers on a stale sheet that matched nothing and the
        # theme silently lost its colours. This document is no-store, so the
        # palette can never be older than the page it styles. ~36 KB, gzipped.
        html = html.replace('<link rel="stylesheet" href="/theme.css">',
                            f"<style>{theme_css}</style>", 1)
        return Response(html, media_type="text/html",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/prefs")
    def api_prefs_get():
        return store.get_prefs(db)

    @app.post("/api/prefs")
    async def api_prefs_set(request: Request):
        body = await _json_body(request)
        key, value = body.get("key"), body.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            raise HTTPException(400, "key and value must be strings")
        store.set_pref(db, key, value)
        return {"ok": True}

    @app.get("/theme.css")
    def theme():
        return Response(theme_css, media_type="text/css",
                        headers={"Cache-Control": "max-age=86400"})

    @app.get("/fonts/{name}")
    def font(name: str):
        p = (STATIC / "fonts" / name).resolve()
        if not p.is_file() or p.parent != (STATIC / "fonts").resolve():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "max-age=31536000"})

    @app.get("/api/files")
    def api_files():
        st.maybe_refresh()
        states = _file_states(st, db)
        files = []
        tot = tot_seen = tot_rev = 0
        for fd in st.files:
            n, seen, rev = _counts(fd, states)
            tot += n; tot_seen += seen; tot_rev += rev
            files.append({
                "path": fd.path, "old_path": fd.old_path, "status": fd.status,
                "added": fd.added, "removed": fd.removed,
                "blocks": n, "seen": seen, "reviewed": rev,
            })
        return {
            "scope": st.scope,
            "branch": st.branch,
            "base_ref": st.base_ref,
            "base_sha": st.mb_sha[:12], "base_date": st.base_date,
            "head_sha": st.head_sha[:12],
            "dirty_files": st.dirty, "conflicted_files": st.conflicted,
            "auto_seen": st.auto_seen,
            "stats": {"blocks": tot, "seen": tot_seen, "reviewed": tot_rev},
            "files": files,
        }

    def _worktree_payload(path: str) -> dict:
        """A walkthrough-only file as an all-context payload, same shape as a
        diff payload so the client needs no branch. In-memory cache keyed by
        (path, mtime): the diff cache's mb-head key only invalidates when HEAD
        moves, which would serve stale content for a file edited in place."""
        try:
            mtime = os.stat(os.path.join(st.repo, path)).st_mtime
        except OSError as e:
            raise HTTPException(404, f"cannot stat {path}: {e}")
        key = (path, mtime)
        if key in st.wt_cache:
            return st.wt_cache[key]
        try:
            rows = G.worktree_rows(st.repo, path)
        except G.GitError as e:
            raise HTTPException(404, str(e))
        toks = None if st.no_highlight else H.highlight_rows(rows, path)
        skip_reason = None if st.no_highlight else H.guard(rows)
        lines = []
        for i, r in enumerate(rows):
            line = {"old": r.old, "new": r.new, "type": r.type}
            if toks is not None:
                line["t"] = toks[i]
            else:
                line["text"] = r.text
            lines.append(line)
        payload = {
            "path": path, "old_path": None, "status": "worktree",
            "lang": (H.lang_for(path) or "text") if toks is not None else "text",
            "total_lines": len(rows),
            "highlighted": toks is not None,
            "skip_reason": skip_reason,
            "blocks": [],
            "lines": lines,
        }
        for k in [k for k in st.wt_cache if k[0] == path]:
            del st.wt_cache[k]       # drop stale versions of this file only
        st.wt_cache[key] = payload
        return payload

    @app.get("/api/walkthrough")
    def api_walkthrough():
        if not st.wt_file:
            raise HTTPException(404, "no walkthrough loaded")
        st.maybe_refresh()
        st.load_walkthrough()
        if st.wt is None and st.wt_empty:
            # placeholder: plain diff mode, client keeps polling for a script
            return {"empty": True}
        if st.wt is None:
            raise HTTPException(
                422, "walkthrough failed to load:\n" + (st.wt_error or "?"))
        out = st.wt.to_json()
        out["error"] = st.wt_error
        out["wt_paths"] = sorted(st.wt_paths)
        return out

    @app.post("/api/walkthrough/dismiss")
    def api_walkthrough_dismiss():
        """Empty the script from the UI — back to the plain diff.

        Truncating the file rather than just hiding the bar is what makes the
        dismissal stick: the agent owns this file and rewrites it for the next
        explanation, and every open tab drops the walkthrough on its next poll.
        """
        if not st.wt_file:
            raise HTTPException(404, "no walkthrough loaded")
        try:
            Path(st.wt_file).write_text("{}\n")
        except OSError as e:
            raise HTTPException(500, f"cannot empty walkthrough: {e}")
        st.load_walkthrough()
        return {"empty": True}

    @app.get("/api/file")
    def api_file(path: str):
        fd = st.by_path.get(path)
        if fd is None:
            if path in st.wt_paths:
                return _worktree_payload(path)
            raise HTTPException(404, f"not in diff: {path}")
        payload = dict(_build_file_payload(st, fd))
        states = _file_states(st, db)
        payload["blocks"] = [
            dict(bm,
                 seen=bool(states.get((path, bm["hunk_id"]), (None, None))[0]),
                 reviewed=bool(states.get((path, bm["hunk_id"]), (None, None))[1]))
            for bm in payload.pop("block_meta")
        ]
        return payload

    @app.get("/api/search")
    def api_search(q: str, regex: bool = False, case: bool = False,
                   limit: int = SEARCH_LIMIT):
        """Content search across every file in the diff.

        The client only ever holds the open file's payload, so a whole-change
        search has to run here. Files go through the same cached builder
        /api/file uses: the first search over a cold diff pays for building
        (and highlighting) every payload, every later one is served from
        memory. One hit per line — this feeds a result list, not a highlighter.
        """
        if not q:
            return {"q": q, "total": 0, "truncated": False, "files": []}
        try:
            rx = re.compile(q if regex else re.escape(q),
                            0 if case else re.IGNORECASE)
        except re.error as e:
            raise HTTPException(400, f"bad regex: {e}")
        limit = max(1, min(limit, 5000))
        st.maybe_refresh()
        out: list[dict] = []
        total = 0
        truncated = False
        for fd in st.files:
            if truncated:
                break
            hits = []
            for i, line in enumerate(_build_file_payload(st, fd)["lines"]):
                m = rx.search(line_text(line))
                if m is None:
                    continue
                hits.append({
                    "line": i,                     # index into payload lines
                    "new": line["new"], "old": line["old"],
                    "type": line["type"],
                    "text": line_text(line)[:400],
                    "start": m.start(), "end": m.end(),
                })
                total += 1
                if total >= limit:
                    truncated = True
                    break
            if hits:
                out.append({"path": fd.path, "matches": hits})
        return {"q": q, "total": total, "truncated": truncated, "files": out}

    def _valid_blocks(items) -> list[tuple[str, str]]:
        out = []
        for it in items or []:
            fd = st.by_path.get(it.get("path", ""))
            if fd and any(b.hunk_id == it.get("hunk_id") for b in fd.blocks):
                out.append((it["path"], it["hunk_id"]))
        return out

    @app.post("/api/seen")
    async def api_seen(request: Request):
        body = await _json_body(request)
        blocks = _valid_blocks(body.get("blocks"))
        if blocks:
            store.mark_seen(db, st.scope, blocks)
        return {"marked": len(blocks)}

    @app.post("/api/reviewed")
    async def api_reviewed(request: Request):
        body = await _json_body(request)
        if "file" in body:
            fd = st.by_path.get(body["file"])
            if fd is None:
                raise HTTPException(404, f"not in diff: {body['file']}")
            blocks = [(fd.path, b.hunk_id) for b in fd.blocks]
        else:
            blocks = _valid_blocks(body.get("blocks"))
        if blocks:
            store.mark_reviewed(db, st.scope, blocks)
        return {"marked": len(blocks)}

    @app.delete("/api/reviewed")
    def api_unreview(path: str | None = None, hunk_id: str | None = None,
                     file: str | None = None, all: bool = False):
        if all:
            store.unmark_reviewed(db, st.scope, all_=True)
        elif file is not None:
            store.unmark_reviewed(db, st.scope, path=file)
        elif path and hunk_id:
            store.unmark_reviewed(db, st.scope, blocks=[(path, hunk_id)])
        else:
            raise HTTPException(400, "need ?all=, ?file= or ?path=&hunk_id=")
        return {"ok": True}

    @app.delete("/api/state")
    def api_reset_state():
        """Wipe seen AND reviewed for this scope (client confirms first)."""
        store.clear_scope(db, st.scope)
        return {"ok": True}

    return app
