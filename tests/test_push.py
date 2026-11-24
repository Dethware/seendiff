"""Pushing from the commit panel: status, push, and when it refuses."""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from seendiff import git as G
from seendiff.server import AppState, create_app

H = {"x-seendiff": "1"}


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def remote_repo(tmp_path, monkeypatch):
    """A clone of a bare remote, one commit ahead of it."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    r = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(bare), str(r))
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-qm", "first")
    _git(r, "push", "-q", "-u", "origin", "main")
    (r / "a.txt").write_text("b\n")
    _git(r, "commit", "-qam", "Second thing")
    return r, bare


def test_push_status_and_push(remote_repo, tmp_path):
    r, bare = remote_repo
    st = AppState(repo=str(r), base_ref="HEAD", db_path=str(tmp_path / "s.db"))
    with TestClient(create_app(st), headers={"host": "localhost"}) as c:
        p = c.get("/api/commit").json()["push"]
        assert (p["upstream"], p["ahead"], p["behind"], p["blocked"]) == \
            ("origin/main", 1, 0, None)
        assert p["commits"][0]["subject"] == "Second thing"
        assert c.post("/api/push").status_code == 403          # needs the header
        res = c.post("/api/push", headers=H)
        assert res.status_code == 200, res.text
        assert res.json()["push"]["ahead"] == 0
        assert c.post("/api/push", headers=H).status_code == 422   # nothing left
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=bare,
                         capture_output=True, text=True).stdout.strip()
    assert log == "Second thing"


def test_new_branch_push_creates_the_upstream(remote_repo):
    r, bare = remote_repo
    _git(r, "checkout", "-qb", "feature")
    (r / "f.txt").write_text("f\n")
    _git(r, "add", "f.txt")
    _git(r, "commit", "-qm", "Feature")
    p = G.push_status(str(r))
    # "Second thing" (main's unpushed commit) rides along: 2, not 1
    assert (p["upstream"], p["remote"], p["ahead"]) == (None, "origin", 2)
    G.push(str(r))
    assert G.push_status(str(r))["upstream"] == "origin/feature"


def test_behind_upstream_blocks_push(remote_repo, tmp_path):
    r, bare = remote_repo
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    (other / "o.txt").write_text("o\n")
    _git(other, "add", "o.txt")
    _git(other, "commit", "-qm", "Theirs")
    _git(other, "push", "-q")
    _git(r, "fetch", "-q")
    p = G.push_status(str(r))
    assert p["behind"] == 1 and "pull first" in p["blocked"]
    with pytest.raises(G.GitError, match="pull first"):
        G.push(str(r))


def test_detached_head_cannot_push(remote_repo):
    r, _ = remote_repo
    _git(r, "checkout", "-q", "--detach")
    assert "detached" in G.push_status(str(r))["blocked"]
