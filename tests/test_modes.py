"""Diff modes: all / committed / staged / unstaged, and live refresh."""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from seendiff import git as G
from seendiff.server import AppState, create_app


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """main has one commit; work has one more, plus staged, unstaged and
    untracked changes on top — one file per layer, and one in two layers."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "c.txt").write_text("c1\nc2\n")
    (r / "s.txt").write_text("s1\ns2\n")
    (r / "u.txt").write_text("u1\nu2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-qb", "work")
    (r / "c.txt").write_text("c1\nC2\n")
    _git(r, "commit", "-qam", "committed change")
    (r / "s.txt").write_text("s1\nS2\n")
    _git(r, "add", "s.txt")
    (r / "s.txt").write_text("s1\nS2\ns3\n")          # staged *and* unstaged
    (r / "u.txt").write_text("U1\nu2\n")
    (r / "new.txt").write_text("brand\nnew\n")       # untracked
    return r


@pytest.fixture
def client(repo, tmp_path):
    st = AppState(repo=str(repo), base_ref="main",
                  db_path=str(tmp_path / "state.db"))
    with TestClient(create_app(st), headers={"host": "localhost"}) as c:
        yield c


def _paths(client, mode):
    return sorted(f["path"] for f in client.get(f"/api/files?mode={mode}").json()["files"])


def test_each_mode_shows_its_layer(client):
    assert _paths(client, "committed") == ["c.txt"]
    assert _paths(client, "staged") == ["s.txt"]
    assert _paths(client, "unstaged") == ["new.txt", "s.txt", "u.txt"]
    assert _paths(client, "all") == ["c.txt", "new.txt", "s.txt", "u.txt"]


def test_default_mode_is_all_and_counts_every_mode(client):
    d = client.get("/api/files").json()
    assert d["mode"] == "all"
    assert d["modes"] == {"all": 4, "committed": 1, "staged": 1, "unstaged": 3}


def test_unknown_mode_is_a_400(client):
    assert client.get("/api/files?mode=nope").status_code == 400


def test_file_rows_come_from_the_right_sides(client):
    def adds(mode, path):
        d = client.get(f"/api/file?path={path}&mode={mode}").json()
        return [l["t"] for l in d["lines"] if l["type"] == "add"]

    text = lambda toks: "".join(t[1] for t in toks)
    assert [text(t) for t in adds("staged", "s.txt")] == ["S2"]
    assert [text(t) for t in adds("unstaged", "s.txt")] == ["s3"]
    assert [text(t) for t in adds("all", "s.txt")] == ["S2", "s3"]
    assert [text(t) for t in adds("unstaged", "new.txt")] == ["brand", "new"]


def test_untracked_files_leave_the_real_index_alone(repo):
    G.identity_diff(str(repo), G.INDEX, G.WORKTREE)
    out = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert "?? new.txt" in out


def test_no_untracked(repo, tmp_path):
    st = AppState(repo=str(repo), base_ref="main", untracked=False,
                  db_path=str(tmp_path / "state.db"))
    assert "new.txt" not in st.snapshot("unstaged").by_path
    assert "new.txt" not in st.snapshot("all").by_path


def test_working_tree_edit_shows_up_without_a_restart(client, repo):
    before = client.get("/api/files?mode=unstaged").json()
    (repo / "u.txt").write_text("U1\nu2\nu3\n")
    after = client.get("/api/files?mode=unstaged").json()
    assert after["version"] != before["version"]
    v = lambda d: next(f["v"] for f in d["files"] if f["path"] == "u.txt")
    assert v(after) != v(before)
    same = lambda d: next(f["v"] for f in d["files"] if f["path"] == "new.txt")
    assert same(after) == same(before)      # untouched file keeps its payload
    d = client.get("/api/file?path=u.txt&mode=unstaged").json()
    assert d["v"] == v(after)
    assert [l["new"] for l in d["lines"] if l["type"] == "add"] == [1, 3]


def test_review_state_follows_a_block_when_it_is_staged(client, repo):
    blk = client.get("/api/file?path=u.txt&mode=unstaged").json()["blocks"][0]
    client.post("/api/reviewed?mode=unstaged",
                json={"blocks": [{"path": "u.txt", "hunk_id": blk["hunk_id"]}]})
    _git(repo, "add", "u.txt")
    staged = client.get("/api/file?path=u.txt&mode=staged").json()["blocks"]
    assert [b["reviewed"] for b in staged] == [True]


def test_seen_is_validated_against_the_named_mode(client):
    blk = client.get("/api/file?path=c.txt&mode=committed").json()["blocks"][0]
    body = {"blocks": [{"path": "c.txt", "hunk_id": blk["hunk_id"]}]}
    assert client.post("/api/seen?mode=staged", json=body).json()["marked"] == 0
    assert client.post("/api/seen?mode=committed", json=body).json()["marked"] == 1


def test_unborn_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    r = tmp_path / "fresh"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("hello\n")
    _git(r, "add", "a.txt")
    (r / "b.txt").write_text("world\n")
    st = AppState(repo=str(r), base_ref="HEAD", db_path=str(tmp_path / "s.db"))
    assert list(st.snapshot("staged").by_path) == ["a.txt"]
    assert sorted(st.snapshot("all").by_path) == ["a.txt", "b.txt"]
    assert st.snapshot("committed").files == []


def test_work_status_parses_renames_and_odd_names(repo):
    _git(repo, "mv", "c.txt", "renamed file.txt")
    (repo / "tab\there.txt").write_text("x\n")
    s = G.work_status(str(repo))
    assert "renamed file.txt" in s.staged
    assert "tab\there.txt" in s.untracked
    assert "c.txt" not in s.staged + s.unstaged + s.untracked


def test_submodule_pointer_suffix_strip():
    fd = G.FileDiff(path="sub", old_path=None, status="submodule", added=1,
                    removed=1, blocks=[G.Block(runs=[G.Run(1, 1, 1, 1, [
                        ("-", "Subproject commit aaaaaaaaad\n"),
                        ("+", "Subproject commit bbbbbbbbbd-dirty\n")])])])
    assert G._pointer_shas(fd) == ("aaaaaaaaad", "bbbbbbbbbd")


def test_conflicted_paths_are_left_out_not_misparsed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    r = tmp_path / "merge"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "f").write_text("a\n")
    (r / "g").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "i")
    _git(r, "checkout", "-qb", "b")
    (r / "f").write_text("b\n")
    _git(r, "commit", "-qam", "b")
    _git(r, "checkout", "-q", "main")
    (r / "f").write_text("c\n")
    _git(r, "commit", "-qam", "c")
    subprocess.run(["git", "merge", "b"], cwd=r, capture_output=True)
    (r / "g").write_text("y\n")
    st = AppState(repo=str(r), base_ref="HEAD", db_path=str(tmp_path / "s.db"))
    for mode in G.MODES:
        assert "f" not in st.snapshot(mode).by_path, mode
    assert list(st.snapshot("unstaged").by_path) == ["g"]
    st.poll_status()
    assert st.conflicted == ["f"]


def _script(path, lines):
    import json
    path.write_text(json.dumps({
        "version": 1, "title": "t", "acts": [{"id": "a", "title": "A"}],
        "steps": [{"id": "s", "act": "a", "title": "T", "text": "x",
                   "ref": {"path": "u.txt", "lines": lines}}]}))


def test_broken_walkthrough_at_startup_is_not_mistaken_for_empty(repo, tmp_path):
    """cli.py exits on `wt is None and not wt_empty`; a script whose refs
    don't resolve used to leave wt_empty True and load as a silent plain diff."""
    wt = tmp_path / "wt.json"
    _script(wt, [9000, 9001])
    st = AppState(repo=str(repo), base_ref="main", wt_file=str(wt),
                  db_path=str(tmp_path / "s.db"))
    st.load_walkthrough()
    assert st.wt is None and not st.wt_empty
    assert "out of range" in st.wt_error


def test_broken_reload_keeps_the_last_good_script(repo, tmp_path):
    import os
    wt = tmp_path / "wt.json"
    _script(wt, [1, 2])
    st = AppState(repo=str(repo), base_ref="main", wt_file=str(wt),
                  db_path=str(tmp_path / "s.db"))
    st.load_walkthrough()
    assert st.wt is not None
    _script(wt, [9000, 9001])
    os.utime(wt, (1, 1))                    # force a new mtime
    st.load_walkthrough()
    assert st.wt is not None and st.wt_error


# ------------------------------------------------------------- committing

H = {"x-seendiff": "1"}


def _log(repo):
    return subprocess.run(["git", "log", "-1", "--format=%s%n%b"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


def test_message_files_are_listed_and_kept_out_of_the_diff(client, repo):
    (repo / "COMMIT_MSG_fix_s.txt").write_text("Fix s\n")
    d = client.get("/api/commit").json()
    assert [m["name"] for m in d["messages"]] == ["COMMIT_MSG_fix_s.txt"]
    assert d["messages"][0]["description"] == "fix s"
    assert [f["path"] for f in d["staged"]] == ["s.txt"]
    assert "COMMIT_MSG_fix_s.txt" not in _paths(client, "unstaged")
    assert client.get("/api/files").json()["modes"]["unstaged"] == 3


def test_commit_takes_staged_only_and_removes_the_proposal(client, repo):
    p = repo / "COMMIT_MSG_fix_s.txt"
    p.write_text("Fix s\n# a note for the reviewer\n")
    m = client.get("/api/commit").json()["messages"][0]
    r = client.post("/api/commit", headers=H, json={
        "name": m["name"], "text": m["text"], "mtime_ns": m["mtime_ns"]})
    assert r.status_code == 200, r.text
    assert r.json()["commit"].endswith(" Fix s")
    assert _log(repo) == "Fix s"                     # '#' line stripped
    assert not p.exists()
    assert _paths(client, "staged") == []
    assert "s.txt" in _paths(client, "unstaged")     # the unstaged part stayed


def test_commit_needs_the_header_and_something_staged(client, repo):
    assert client.post("/api/commit", json={"name": None, "text": "x"}).status_code == 403
    _git(repo, "restore", "--staged", "s.txt")
    r = client.post("/api/commit", headers=H, json={"name": None, "text": "x"})
    assert r.status_code == 409


def test_stale_message_file_is_not_committed_or_overwritten(client, repo):
    p = repo / "COMMIT_MSG_x.txt"
    p.write_text("Old\n")
    m = client.get("/api/commit").json()["messages"][0]
    p.write_text("Rewritten by the agent\n")
    import os
    later = int(m["mtime_ns"]) + 10**9
    os.utime(p, ns=(later, later))
    body = {"name": m["name"], "text": "Old\n", "mtime_ns": m["mtime_ns"]}
    assert client.post("/api/commit", headers=H, json=body).status_code == 409
    assert client.put("/api/commit/message", headers=H, json=body).status_code == 409
    assert p.read_text() == "Rewritten by the agent\n"


def test_save_creates_and_updates_a_message(client, repo):
    body = {"name": "COMMIT_MSG_new_one.txt", "text": "New thing\n", "mtime_ns": None}
    r = client.put("/api/commit/message", headers=H, json=body)
    assert r.status_code == 200
    assert (repo / "COMMIT_MSG_new_one.txt").read_text() == "New thing\n"
    body.update(text="Newer thing\n", mtime_ns=r.json()["mtime_ns"])
    assert client.put("/api/commit/message", headers=H, json=body).status_code == 200
    assert (repo / "COMMIT_MSG_new_one.txt").read_text() == "Newer thing\n"


def test_message_names_cannot_escape_the_pattern(client):
    for bad in ["../COMMIT_MSG_x.txt", "COMMIT_MSG_x.txt/..", "notes.txt",
                "sub/COMMIT_MSG_x.txt", "COMMIT_MSG_x.md"]:
        r = client.put("/api/commit/message", headers=H,
                       json={"name": bad, "text": "x", "mtime_ns": None})
        assert r.status_code == 400, bad


def test_failing_hook_reports_its_output(client, repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    r = client.post("/api/commit", headers=H, json={"name": None, "text": "x"})
    assert r.status_code == 422
    assert "lint says no" in r.json()["detail"]


def test_mtime_stamps_survive_a_javascript_round_trip(client, repo):
    """JS numbers are doubles: an integer nanosecond stamp came back rounded
    and every save/commit from the browser failed with 'changed on disk'."""
    (repo / "COMMIT_MSG_x.txt").write_text("X\n")
    m = client.get("/api/commit").json()["messages"][0]
    assert isinstance(m["mtime_ns"], str)
    body = {"name": m["name"], "text": "X\n", "mtime_ns": m["mtime_ns"]}
    assert client.post("/api/commit", headers=H, json=body).status_code == 200


# ------------------------------------------------------------- presence

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_presence_waits_for_a_first_tab():
    from seendiff.server import Presence
    clk = _Clock()
    p = Presence(clock=clk)
    clk.t += 10_000
    assert not p.idle()                       # no tab has ever connected


def test_presence_exits_after_the_last_tab_and_grace():
    from seendiff.server import Presence
    clk = _Clock()
    p = Presence(clock=clk)
    p.hello("tab-a")
    p.hello("tab-b")
    p.bye("tab-a")
    clk.t += 600                              # a stream can stay open for hours
    assert not p.idle()
    p.bye("tab-b")
    clk.t += Presence.GRACE - 0.5
    assert not p.idle()
    clk.t += 1
    assert p.idle()


def test_presence_reload_within_grace_keeps_running():
    from seendiff.server import Presence
    clk = _Clock()
    p = Presence(clock=clk)
    p.hello("old-page")
    p.bye("old-page")
    clk.t += 0.5
    p.hello("new-page")
    clk.t += Presence.GRACE * 10
    assert not p.idle()


def test_presence_unknown_bye_is_ignored():
    from seendiff.server import Presence
    clk = _Clock()
    p = Presence(clock=clk)
    p.hello("real-tab")
    p.bye("nobody")
    clk.t += 100
    assert not p.idle()


def test_event_stream_is_a_tabs_presence(repo, tmp_path):
    """Opening /api/events registers a tab; dropping the connection is the
    tab closing — no goodbye message needed."""
    import threading, time, socket, uvicorn
    st = AppState(repo=str(repo), base_ref="main",
                  db_path=str(tmp_path / "state.db"))
    app = create_app(st)
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error",
                                           timeout_graceful_shutdown=1))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        conn = socket.create_connection(("127.0.0.1", port))
        conn.sendall(b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
        head = conn.recv(4096).decode()
        assert "text/event-stream" in head
        if "event: hello" not in head:
            head += conn.recv(4096).decode()
        assert "event: hello" in head and '"instance"' in head
        assert len(st.presence.clients) == 1 and not st.presence.idle()
        conn.close()                           # the tab goes away, no bye
        for _ in range(50):
            if not st.presence.clients:
                break
            time.sleep(0.05)
        assert not st.presence.clients
        assert st.presence.empty_since is not None
    finally:
        server.should_exit = True
        th.join(5)
