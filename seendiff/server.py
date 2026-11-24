"""FastAPI app: API routes, caches, theme.css generation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
BLOB_LIMIT = 50_000_000  # bytes an image side may have before /api/blob refuses
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


class Presence:
    """Which browser tabs are open, so the server can exit with the last one.

    Each tab holds one /api/events stream open for as long as it lives; the
    stream's lifetime *is* the tab's presence. A closed tab drops its socket,
    which the server notices within a fraction of a second whatever the
    browser does on the way out (some embedded browsers never fire pagehide).

    The server is idle once it has seen at least one tab, none is left, and
    GRACE seconds have passed: just enough for a reload to reconnect.
    """
    GRACE = 2.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = Lock()
        self.clients: set[str] = set()
        self.armed = False             # nothing to wait for until a tab shows up
        self.empty_since: float | None = None

    def hello(self, cid: str) -> None:
        with self.lock:
            self.clients.add(cid)
            self.armed = True
            self.empty_since = None

    def bye(self, cid: str) -> None:
        with self.lock:
            if cid in self.clients:
                self.clients.discard(cid)
                if not self.clients:
                    self.empty_since = self.clock()

    def idle(self) -> bool:
        with self.lock:
            return (self.armed and not self.clients
                    and self.empty_since is not None
                    and self.clock() - self.empty_since >= self.GRACE)


@dataclass
class Snapshot:
    """One mode's diff at one moment. `key` moves whenever the diff does."""
    mode: str
    key: str
    files: list[G.FileDiff]
    by_path: dict[str, G.FileDiff]
    disk_dir: Path | None = None     # only immutable (commit → commit) diffs

    def paths(self) -> set[str]:
        return set(self.by_path)


@dataclass
class AppState:
    repo: str
    base_ref: str
    no_highlight: bool = False
    auto_seen: bool = True
    section_min: int = G.SECTION_MIN
    section_max: int = G.SECTION_MAX
    db_path: str | None = None
    default_mode: str = G.DEFAULT_MODE
    untracked: bool = True
    presence: Presence = field(default_factory=Presence)
    # set when the server begins shutting down: open /api/events streams end
    # themselves on it, instead of being cancelled (with tracebacks) later
    stopping: Event = field(default_factory=Event)

    mb_sha: str = ""
    head_sha: str = ""
    base_date: str = ""
    branch: str | None = None
    scope: str = ""
    status: G.WorkStatus = field(default_factory=G.WorkStatus)
    snaps: dict[str, Snapshot] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)
    # payloads keyed by (path, fingerprint): content-addressed, so they are
    # valid for any mode and need no invalidation, only pruning
    file_cache: dict[tuple[str, str], dict] = field(default_factory=dict)

    # walkthrough mode (DEVELOPMENT.md, "Walkthroughs")
    wt_file: str | None = None                 # path of the JSON, if any
    wt: W.Walkthrough | None = None            # last good copy
    wt_empty: bool = True                      # file absent/empty: plain diff
    wt_error: str | None = None                # why the latest reload failed
    wt_attempt_mtime: float = -1.0             # last mtime we tried to load
    wt_cache: dict[tuple[str, float], dict] = field(default_factory=dict)

    # ---- compatibility views onto the default mode (walkthrough, tests) ----
    @property
    def files(self) -> list[G.FileDiff]:
        return self.snapshot(self.default_mode, fresh=False).files

    @property
    def by_path(self) -> dict[str, G.FileDiff]:
        return self.snapshot(self.default_mode, fresh=False).by_path

    @property
    def dirty(self) -> list[str]:
        return self.status.dirty

    @property
    def conflicted(self) -> list[str]:
        return self.status.conflicted

    def hunk_ids(self) -> dict[str, set[str]]:
        return {p: {b.hunk_id for b in fd.blocks}
                for p, fd in self.by_path.items()}

    def wt_paths(self, snap: Snapshot) -> set[str]:
        """Walkthrough files that are not in this mode's diff."""
        return self.wt.paths() - snap.paths() if self.wt else set()

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
        except W.WalkthroughError as e:
            self.wt_error = "\n".join(e.problems)
            if self.wt is None:
                # no last good copy to fall back on: this is a broken script,
                # not the empty one — without this the CLI's fail-loudly check
                # never fires and the error is silently swallowed
                self.wt_empty = False

    def _refresh_head(self) -> None:
        """Re-derive everything keyed on HEAD, if HEAD moved."""
        head = G.head_or_empty(self.repo)
        if head == self.head_sha and self.mb_sha:
            return
        self.head_sha = head
        try:
            self.mb_sha = G.merge_base(self.repo, self.base_ref)
        except G.GitError:
            self.mb_sha = head           # unborn branch, or base_ref is HEAD
        try:
            self.base_date = G.commit_date(self.repo, self.mb_sha)
        except G.GitError:
            self.base_date = ""
        self.branch = G.branch_name(self.repo)
        self.scope = store.scope_key(self.repo, self.base_ref, self.branch,
                                     self.mb_sha)

    def snapshot(self, mode: str, fresh: bool = True) -> Snapshot:
        """This mode's diff. fresh=False reuses any copy already computed.

        Committed diffs are immutable per (merge base, HEAD), so they are only
        recomputed when HEAD moves. The other modes include the index or the
        working tree, which change under us with no HEAD move, so a fresh
        snapshot always re-runs the (cheap, -U0) diff and keeps the previous
        object only if the patch text came out identical.
        """
        with self.lock:
            prev = self.snaps.get(mode)
            if prev is not None and not fresh:
                return prev
            self._refresh_head()
            old, new = G.mode_sides(mode, self.mb_sha, self.head_sha)
            if mode == "committed":
                key = f"{self.mb_sha}-{self.head_sha}"
                if prev is not None and prev.key == key:
                    return prev
                raw = G.raw_diff(self.repo, old, new)
            else:
                raw = G.raw_diff(self.repo, old, new, self.untracked)
                key = hashlib.sha256(
                    f"{old}\0{new}\0{raw}".encode("utf-8", "replace")
                ).hexdigest()[:24]
                if prev is not None and prev.key == key:
                    return prev
            files = G.expand_submodules(
                self.repo,
                G.parse_diff(raw, old, new, self.section_min, self.section_max),
                self.section_min, self.section_max)
            # the v prefix is the payload format: bump it when payloads change
            # shape, or runs of an older version serve their stale copies
            disk = (cache_root()
                    / f"v2-{self.mb_sha[:12]}-{self.head_sha[:12]}"
                    if mode == "committed" else None)
            snap = Snapshot(mode=mode, key=key, files=files,
                            by_path={f.path: f for f in files}, disk_dir=disk)
            self.snaps[mode] = snap
            self._prune_file_cache()
            return snap

    def _prune_file_cache(self) -> None:
        live = {(f.path, f.fingerprint)
                for sn in self.snaps.values() for f in sn.files}
        for k in [k for k in self.file_cache if k not in live]:
            del self.file_cache[k]

    def refresh(self) -> None:
        """Startup: the default mode's diff and the status."""
        self.snapshot(self.default_mode)
        self.poll_status()

    def poll_status(self) -> None:
        self.status = G.work_status(self.repo)

    def mode_counts(self) -> dict[str, int]:
        """Changed-file counts for the mode tabs, without diffing all four.

        staged / unstaged come straight from `git status`; committed is cheap
        (cached until HEAD moves); "all" is their union, which only
        overcounts a file whose uncommitted edits exactly undo its commits.
        """
        committed = self.snapshot("committed").paths()
        s = self.status
        unstaged = set(s.unstaged) | (set(s.untracked) if self.untracked else set())
        return {
            "all": len(committed | set(s.staged) | unstaged),
            "committed": len(committed),
            "staged": len(s.staged),
            "unstaged": len(unstaged),
        }


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


def _weight(b: G.Block) -> int:
    """A block's share of the progress bar: its changed lines, never zero.

    Counting blocks instead made the bar lie in both directions — a two-line
    fix weighed the same as a two-hundred-line rewrite.
    """
    return (b.added + b.removed) or 1


def _counts(fd: G.FileDiff, states) -> tuple[int, int, int, int, int, int]:
    seen = reviewed = 0
    w_tot = w_seen = w_rev = 0
    for b in fd.blocks:
        w = _weight(b)
        w_tot += w
        s, r = states.get((fd.path, b.hunk_id), (None, None))
        if r:
            reviewed += 1
            w_rev += w
        elif s:
            seen += 1
            w_seen += w
    return len(fd.blocks), seen, reviewed, w_tot, w_seen, w_rev


def _build_file_payload(st: AppState, fd: G.FileDiff,
                        snap: Snapshot | None = None) -> dict:
    """The immutable part of /api/file (rows + tokens).

    Cached in memory by (path, fingerprint) — the fingerprint hashes the
    file's patch text, blob ids included, so it changes exactly when either
    side does. Committed diffs are also cached on disk across runs.
    """
    ck = (fd.path, fd.fingerprint)
    if ck in st.file_cache:
        return st.file_cache[ck]
    disk = None
    if snap is not None and snap.disk_dir is not None:
        key = hashlib.sha256(fd.path.encode()).hexdigest()[:16] + ".json"
        disk = snap.disk_dir / key
        if disk.is_file():
            try:
                payload = json.loads(disk.read_text())
                st.file_cache[ck] = payload
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
        "v": fd.fingerprint,
        "lang": (H.lang_for(fd.path) or "text") if toks is not None else "text",
        "total_lines": len(rows),
        "highlighted": toks is not None,
        "skip_reason": skip_reason,
        "image": _image_meta(st, fd),
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
    st.file_cache[ck] = payload
    if disk is not None:
        try:
            disk.parent.mkdir(parents=True, exist_ok=True)
            disk.write_text(json.dumps(payload))
        except OSError:
            pass
    return payload


def _image_meta(st: AppState, fd: G.FileDiff) -> dict | None:
    """For an image file: its type, and each side's size in bytes (None where
    the side doesn't exist). The client fetches the pictures from /api/blob."""
    mime = G.image_type(fd.path)
    if mime is None or fd.status not in ("binary", "renamed", "meta"):
        return None
    sizes: dict[str, int | None] = {}
    for name, where in G.diff_sides(fd, st.repo).items():
        if where is None:
            sizes[name] = None
            continue
        try:
            sizes[name] = len(G.show_side_bytes(*where, BLOB_LIMIT))
        except G.GitError:
            sizes[name] = None
    return {"type": mime, "old": sizes["old"], "new": sizes["new"]}


def line_text(line: dict) -> str:
    """The plain text of a payload line, highlighted or not."""
    toks = line.get("t")
    if toks is not None:
        return "".join(piece[1] for piece in toks)
    return line.get("text") or ""


def _mode(st: AppState, mode: str | None) -> str:
    if mode is None:
        return st.default_mode
    if mode not in G.MODES:
        raise HTTPException(400, f"unknown mode {mode!r}; one of "
                            + ", ".join(G.MODES))
    return mode


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
    instance = secrets.token_hex(8)        # names this process to its tabs
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

    @app.get("/api/events")
    async def api_events(request: Request):
        """The tab's live line to the server (Presence), as Server-Sent Events.

        Opens with a `hello` naming this server process, so a tab that lost
        its server and reconnects to a *new* one on the same port knows to
        reload rather than act on stale state. A comment line every 15 s
        keeps idle proxies and half-open sockets honest.
        """
        cid = secrets.token_hex(8)

        async def stream():
            st.presence.hello(cid)
            try:
                yield ("retry: 1000\nevent: hello\n"
                       f"data: {json.dumps({'instance': instance})}\n\n")
                last = time.monotonic()
                while not (st.stopping.is_set()
                           or await request.is_disconnected()):
                    await asyncio.sleep(0.2)
                    if time.monotonic() - last >= 15:
                        last = time.monotonic()
                        yield ": ping\n\n"
            finally:
                # also runs when the response is cancelled (disconnect noticed
                # by Starlette first, or shutdown)
                st.presence.bye(cid)

        return StreamingResponse(stream(), media_type="text/event-stream",
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
    def api_files(mode: str | None = None):
        """The file list for one mode. The client polls this: it is also how
        an open tab notices the working tree, the index or HEAD moving."""
        mode = _mode(st, mode)
        snap = st.snapshot(mode)
        st.poll_status()
        states = _file_states(st, db)
        files = []
        tot = tot_seen = tot_rev = 0
        w_tot = w_seen = w_rev = 0
        for fd in snap.files:
            n, seen, rev, wt, ws, wr = _counts(fd, states)
            tot += n; tot_seen += seen; tot_rev += rev
            w_tot += wt; w_seen += ws; w_rev += wr
            files.append({
                "path": fd.path, "old_path": fd.old_path, "status": fd.status,
                "v": fd.fingerprint,
                "added": fd.added, "removed": fd.removed,
                "blocks": n, "seen": seen, "reviewed": rev,
                "weight": wt, "seen_w": ws, "reviewed_w": wr,
            })
        return {
            "mode": mode, "default_mode": st.default_mode,
            "modes": st.mode_counts(),
            "version": snap.key,
            "repo": st.repo,
            "scope": st.scope,
            "branch": st.branch,
            "base_ref": st.base_ref,
            "base_sha": st.mb_sha[:12], "base_date": st.base_date,
            "head_sha": st.head_sha[:12],
            "dirty_files": st.dirty, "conflicted_files": st.conflicted,
            "auto_seen": st.auto_seen,
            "stats": {"blocks": tot, "seen": tot_seen, "reviewed": tot_rev,
                      "weight": w_tot, "seen_w": w_seen, "reviewed_w": w_rev},
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
    def api_walkthrough(mode: str | None = None):
        if not st.wt_file:
            raise HTTPException(404, "no walkthrough loaded")
        snap = st.snapshot(_mode(st, mode), fresh=False)
        st.load_walkthrough()
        if st.wt is None and st.wt_empty:
            # placeholder: plain diff mode, client keeps polling for a script
            return {"empty": True}
        if st.wt is None:
            raise HTTPException(
                422, "walkthrough failed to load:\n" + (st.wt_error or "?"))
        out = st.wt.to_json()
        out["error"] = st.wt_error
        out["wt_paths"] = sorted(st.wt_paths(snap))
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
    def api_file(path: str, mode: str | None = None):
        snap = st.snapshot(_mode(st, mode), fresh=False)
        fd = snap.by_path.get(path)
        if fd is None:
            st.load_walkthrough()
            if path in st.wt_paths(snap):
                return _worktree_payload(path)
            raise HTTPException(404, f"not in diff: {path}")
        payload = dict(_build_file_payload(st, fd, snap))
        payload["v"] = fd.fingerprint   # disk copies from older versions lack it
        states = _file_states(st, db)
        payload["blocks"] = [
            dict(bm,
                 seen=bool(states.get((path, bm["hunk_id"]), (None, None))[0]),
                 reviewed=bool(states.get((path, bm["hunk_id"]), (None, None))[1]))
            for bm in payload.pop("block_meta")
        ]
        return payload

    @app.get("/api/blob")
    def api_blob(path: str, side: str, mode: str | None = None):
        """One side of an image in the diff, as raw bytes.

        Only for files whose extension is an image type: this serves file
        content verbatim, and a page navigated to it should never execute
        anything (the sandbox CSP and nosniff are belt and braces; the client
        only ever loads these through <img>). Cacheable forever: the client
        puts the file's fingerprint in the URL.
        """
        snap = st.snapshot(_mode(st, mode), fresh=False)
        fd = snap.by_path.get(path)
        mime = G.image_type(path)
        if fd is None or mime is None or side not in ("old", "new"):
            raise HTTPException(404, f"no image side {side!r} for {path}")
        where = G.diff_sides(fd, st.repo)[side]
        if where is None:
            raise HTTPException(404, f"{path} has no {side} side")
        try:
            data = G.show_side_bytes(*where, BLOB_LIMIT)
        except G.GitError as e:
            raise HTTPException(404, str(e))
        return Response(data, media_type=mime, headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/search")
    def api_search(q: str, regex: bool = False, case: bool = False,
                   limit: int = SEARCH_LIMIT, mode: str | None = None):
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
        snap = st.snapshot(_mode(st, mode), fresh=False)
        out: list[dict] = []
        total = 0
        truncated = False
        for fd in snap.files:
            if truncated:
                break
            if not fd.has_content:
                continue          # a PNG has no lines to match, and no payload
            hits = []
            for i, line in enumerate(_build_file_payload(st, fd, snap)["lines"]):
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

    def _valid_blocks(items, mode: str | None) -> list[tuple[str, str]]:
        snap = st.snapshot(_mode(st, mode), fresh=False)
        out = []
        for it in items or []:
            fd = snap.by_path.get(it.get("path", ""))
            if fd and any(b.hunk_id == it.get("hunk_id") for b in fd.blocks):
                out.append((it["path"], it["hunk_id"]))
        return out

    @app.post("/api/seen")
    async def api_seen(request: Request, mode: str | None = None):
        body = await _json_body(request)
        blocks = _valid_blocks(body.get("blocks"), mode)
        if blocks:
            store.mark_seen(db, st.scope, blocks)
        return {"marked": len(blocks)}

    @app.post("/api/reviewed")
    async def api_reviewed(request: Request, mode: str | None = None):
        body = await _json_body(request)
        if "file" in body:
            fd = st.snapshot(_mode(st, mode), fresh=False).by_path.get(body["file"])
            if fd is None:
                raise HTTPException(404, f"not in diff: {body['file']}")
            blocks = [(fd.path, b.hunk_id) for b in fd.blocks]
        else:
            blocks = _valid_blocks(body.get("blocks"), mode)
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

    # ---- committing: proposed message files + what is staged ----
    def _need_action_header(request: Request) -> None:
        # These change the repo, unlike review marks. A page on another site
        # can POST a form to 127.0.0.1:<port> with an acceptable Host header;
        # it cannot add a custom header without a CORS preflight, which this
        # server never answers.
        if request.headers.get("x-seendiff") != "1":
            raise HTTPException(403, "missing X-Seendiff header")

    def _msg_path(name) -> str:
        if not isinstance(name, str) or not G.COMMIT_MSG_RE.fullmatch(name):
            raise HTTPException(400, "message files are named "
                                "COMMIT_MSG_<description>.txt")
        return os.path.join(st.repo, name)

    def _check_unchanged(path: str, mtime_ns) -> None:
        """409 if the file moved since the client read it (an agent or the
        IDE rewrote it): never overwrite or commit a stale copy silently."""
        try:
            cur = str(os.stat(path).st_mtime_ns)
        except FileNotFoundError:
            cur = None
        if cur != (None if mtime_ns is None else str(mtime_ns)):
            raise HTTPException(409, "the message file changed on disk "
                                "since it was loaded; reload it")

    @app.get("/api/commit")
    def api_commit_state():
        st.poll_status()
        return {
            "branch": st.branch,
            "messages": G.commit_messages(st.repo),
            "staged": G.staged_changes(st.repo),
            "unstaged": len(st.status.unstaged) + len(st.status.untracked),
            "conflicted": st.status.conflicted,
            "push": G.push_status(st.repo),
        }

    @app.post("/api/push")
    def api_push(request: Request):
        """Push the current branch to its upstream (or create one). A plain
        def: git push can take a while, and runs in the thread pool."""
        _need_action_header(request)
        try:
            report = G.push(st.repo)
        except G.GitError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "report": report, "push": G.push_status(st.repo)}

    @app.put("/api/commit/message")
    async def api_commit_save(request: Request):
        _need_action_header(request)
        body = await _json_body(request)
        path = _msg_path(body.get("name"))
        text = body.get("text")
        if not isinstance(text, str) or len(text) > G.COMMIT_MSG_MAX:
            raise HTTPException(400, "text must be a string under 64 kB")
        _check_unchanged(path, body.get("mtime_ns"))
        try:
            Path(path).write_text(text, encoding="utf-8")
            mtime_ns = str(os.stat(path).st_mtime_ns)
        except OSError as e:
            raise HTTPException(500, f"cannot write {body['name']}: {e}")
        return {"name": body["name"], "mtime_ns": mtime_ns}

    @app.post("/api/commit")
    async def api_commit(request: Request):
        _need_action_header(request)
        body = await _json_body(request)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "empty commit message")
        name = body.get("name")
        path = _msg_path(name) if name is not None else None
        if path:
            _check_unchanged(path, body.get("mtime_ns"))
        if not G.staged_changes(st.repo):
            raise HTTPException(409, "nothing is staged")
        try:
            summary = G.commit(st.repo, text)
        except G.GitError as e:
            raise HTTPException(422, str(e))
        removed = False
        if path:
            # the message now lives in the commit; the proposal is done
            try:
                os.remove(path)
                removed = True
            except OSError:
                pass
        return {"ok": True, "commit": summary, "removed": name if removed else None}

    @app.delete("/api/state")
    def api_reset_state():
        """Wipe seen AND reviewed for this scope (client confirms first)."""
        store.clear_scope(db, st.scope)
        return {"ok": True}

    return app
