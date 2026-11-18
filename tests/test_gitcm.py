"""git cm: the packaged terminal twin of seendiff's commit panel."""
from __future__ import annotations

import subprocess

import pytest

from seendiff import gitcm


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "f").write_text("a\n")
    _git(r, "add", "f")
    _git(r, "commit", "-qm", "init")
    (r / "sub").mkdir()
    monkeypatch.chdir(r / "sub")              # works from anywhere in the repo
    monkeypatch.setenv("GIT_EDITOR", "true")  # "save" the proposal unchanged
    return r


def test_commits_the_single_proposal_and_removes_it(repo):
    (repo / "COMMIT_MSG_fix_f.txt").write_text("Fix f\n# note\n")
    (repo / "f").write_text("b\n")
    _git(repo, "add", "f")
    assert gitcm.main([]) == 0
    assert _git(repo, "log", "-1", "--format=%B").strip() == "Fix f"
    assert not (repo / "COMMIT_MSG_fix_f.txt").exists()


def test_several_proposals_need_a_word(repo, capsys):
    (repo / "COMMIT_MSG_one.txt").write_text("One\n")
    (repo / "COMMIT_MSG_two.txt").write_text("Two\n")
    (repo / "f").write_text("b\n")
    _git(repo, "add", "f")
    assert gitcm.main([]) == 1
    assert "several proposals" in capsys.readouterr().err
    assert gitcm.main(["two"]) == 0
    assert _git(repo, "log", "-1", "--format=%s").strip() == "Two"
    assert (repo / "COMMIT_MSG_one.txt").exists()


def test_refuses_with_nothing_staged(repo, capsys):
    (repo / "COMMIT_MSG_x.txt").write_text("X\n")
    assert gitcm.main([]) == 1
    assert "nothing is staged" in capsys.readouterr().err


def test_aborted_commit_keeps_the_proposal(repo, monkeypatch):
    (repo / "COMMIT_MSG_x.txt").write_text("X\n")
    (repo / "f").write_text("b\n")
    _git(repo, "add", "f")
    monkeypatch.setenv("GIT_EDITOR", "sh -c ': > \"$1\"' --")   # empties it
    assert gitcm.main([]) != 0
    assert (repo / "COMMIT_MSG_x.txt").exists()


def test_list(repo, capsys):
    (repo / "COMMIT_MSG_x.txt").write_text("# note\nSubject here\n")
    assert gitcm.main(["-l"]) == 0
    assert "Subject here" in capsys.readouterr().out
