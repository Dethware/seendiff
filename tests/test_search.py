"""/api/search: whole-change content search (see README §search)."""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from seendiff import server
from seendiff.server import AppState, create_app, line_text


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A two-commit repo whose diff has something to find."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("def alpha():\n    return 1\n")
    (repo / "b.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "work")
    (repo / "a.py").write_text(
        "def alpha():\n    return NEEDLE\n\ndef beta():\n    return needle\n")
    (repo / "b.py").write_text("x = 1\ny = NEEDLE\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")

    st = AppState(repo=str(repo), base_ref="main",
                  db_path=str(tmp_path / "state.db"))
    with TestClient(create_app(st), headers={"host": "localhost"}) as c:
        yield c


def test_every_theme_has_syntax_rules(client):
    """A theme with no matching selectors renders as plain text.

    Renaming a theme rewrites every selector in the generated palette, so this
    guards the pairing between THEME_STYLES and the data-theme values the
    viewer actually sets.
    """
    html = client.get("/").text
    for theme in server.THEME_STYLES:
        assert f'[data-theme="{theme}"] .hl .k' in html, f"{theme}: no keyword rule"
        assert f'data-theme="{theme}"' in html, f"{theme}: no palette in index.html"


def test_theme_palette_is_inlined_not_linked(client):
    """Linked, it was cached for a day and could outlive a theme rename."""
    r = client.get("/")
    assert 'href="/theme.css"' not in r.text, "linked again; it can go stale"
    assert "no-store" in r.headers["cache-control"]
    # the palette must still land before the app's own rules, which override
    # some of it at equal specificity
    assert r.text.index(".hl .k ") < r.text.index("#sb-progress {")


def hits(payload):
    return [(f["path"], m["new"], m["text"])
            for f in payload["files"] for m in f["matches"]]


def test_finds_across_files(client):
    d = client.get("/api/search", params={"q": "NEEDLE"}).json()
    assert d["total"] == 3            # smart case is the client's job, not ours
    assert {p for p, _, _ in hits(d)} == {"a.py", "b.py"}
    assert not d["truncated"]


def test_case_sensitive(client):
    d = client.get("/api/search", params={"q": "NEEDLE", "case": "true"}).json()
    assert [t for _, _, t in hits(d)] == ["    return NEEDLE", "y = NEEDLE"]


def test_line_numbers_point_at_the_payload(client):
    d = client.get("/api/search", params={"q": "y = NEEDLE"}).json()
    (path, new, text), = hits(d)
    lines = client.get("/api/file", params={"path": path}).json()["lines"]
    m, = [m for f in d["files"] for m in f["matches"]]
    assert line_text(lines[m["line"]]) == text
    assert lines[m["line"]]["new"] == new
    assert text[m["start"]:m["end"]] == "y = NEEDLE"


def test_regex(client):
    d = client.get("/api/search",
                   params={"q": r"def \w+\(", "regex": "true"}).json()
    assert sorted(t for _, _, t in hits(d)) == ["def alpha():", "def beta():"]


def test_regex_off_treats_the_query_as_literal(client):
    assert client.get("/api/search", params={"q": r"def \w+\("}).json()["total"] == 0


def test_bad_regex_is_a_400(client):
    r = client.get("/api/search", params={"q": "a(", "regex": "true"})
    assert r.status_code == 400


def test_empty_query(client):
    d = client.get("/api/search", params={"q": ""}).json()
    assert d == {"q": "", "total": 0, "truncated": False, "files": []}


def test_limit_truncates(client):
    d = client.get("/api/search", params={"q": "e", "limit": 2}).json()
    assert d["truncated"] and d["total"] == 2


def test_one_hit_per_line(client):
    """One result row per line, and removed lines are searchable too.

    "return" hits three payload lines in a.py: the removed "return 1" and both
    added ones. A reviewer looking for deleted code needs that.
    """
    d = client.get("/api/search", params={"q": "return"}).json()
    rows = [m for f in d["files"] for m in f["matches"]]
    assert len(rows) == len({m["line"] for m in rows}) == 3
    assert {m["type"] for m in rows} == {"add", "del"}
