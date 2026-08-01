"""FastAPI app: API routes, caches, theme.css generation (PLAN.md §8)."""
from __future__ import annotations

import hashlib
import json
import os
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

STATIC = Path(__file__).parent / "static"

THEME_STYLES = {
    "light": "default",
    "dark": "github-dark",
    "solarized": "solarized-dark",
    "space": "nord",
    "hailmary": "coffee",
}

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
if os.environ.get("SEENDIFF_ALLOW_HOST"):        # test/dev escape hatch only
    _ALLOWED_HOSTS.add(os.environ["SEENDIFF_ALLOW_HOST"])
CACHE_PRUNE_DAYS = 30


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
        return FileResponse(STATIC / "index.html")

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

    @app.get("/api/file")
    def api_file(path: str):
        fd = st.by_path.get(path)
        if fd is None:
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
