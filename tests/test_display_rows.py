"""display_rows: rows are replayed from the -U0 runs, so they always tile.

The bug this pins: rows used to come from a second `git diff --unified=999999`,
and git's edit script is not stable across context widths — a line identical on
both sides can be context under `-U0` and a delete/add pair at full context.
The blocks then covered 52 changed rows while the display had 54, and the two
strays raised out of verify_tagging as a 500 that killed the whole file view.
"""
from __future__ import annotations

import subprocess

import pytest

from seendiff import git as G


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    """One repo, four hunk shapes: pure delete, pure insert, mixed, EOF delete."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")

    old = [f"line {i}" for i in range(1, 61)]
    (r / "f.py").write_text("\n".join(old) + "\n")
    (r / "gone.py").write_text("dropped\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-qb", "work")

    new = list(old)
    del new[55:]            # pure deletion at EOF
    new[30:31] = ["line 31 rewritten"]          # mixed 1:1
    new[20:20] = ["inserted a", "inserted b"]   # pure insertion
    del new[5:9]                                # pure deletion mid-file
    (r / "f.py").write_text("\n".join(new) + "\n")
    (r / "gone.py").unlink()
    (r / "added.py").write_text("brand new\nsecond line\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "work")
    return r


def _files(repo):
    mb = G.merge_base(str(repo), "main")
    return mb, G.identity_diff(str(repo), mb)


def test_rows_rebuild_both_sides(repo):
    """Context+add is the new file, context+del is the old one, exactly."""
    mb, fds = _files(repo)
    seen = set()
    for fd in fds:
        rows = G.display_rows(str(repo), mb, fd)
        seen.add(fd.path)
        want_new = ([] if fd.status == "deleted"
                    else G._split_text(G.show_file(str(repo), "HEAD", fd.path)))
        want_old = ([] if fd.status == "added"
                    else G._split_text(G.show_file(str(repo), mb, fd.path)))
        assert [r.text for r in rows if r.type in ("context", "add")] == want_new
        assert [r.text for r in rows if r.type in ("context", "del")] == want_old
    assert seen == {"f.py", "gone.py", "added.py"}


def test_every_changed_row_belongs_to_a_block(repo):
    """The invariant verify_tagging guards, on all four hunk shapes at once."""
    mb, fds = _files(repo)
    for fd in fds:
        rows = G.display_rows(str(repo), mb, fd)
        untagged = [r for r in rows if r.type != "context" and r.hunk_id is None]
        assert not untagged, f"{fd.path}: {len(untagged)} untagged"
        G.verify_tagging(rows, fd.blocks, fd.path)


def test_line_numbers_advance_by_one(repo):
    """Both sides number continuously — nothing skipped, nothing repeated."""
    mb, fds = _files(repo)
    for fd in fds:
        old = new = 0
        for r in G.display_rows(str(repo), mb, fd):
            if r.old is not None:
                assert r.old == old + 1
                old = r.old
            if r.new is not None:
                assert r.new == new + 1
                new = r.new


def test_zero_length_sides_anchor_after_the_line_they_name():
    """git writes `+N,0` for a deletion sitting *after* new line N (and the
    mirror for `-N,0`), which is the one place row replay can slip by one."""
    text = "a\nb\nc\nd\n"
    # @@ -2,2 +1,0 @@ — old lines 2 and 3 removed, after new line 1
    dele = G.Run(src_start=2, src_len=2, tgt_start=1, tgt_len=0,
                 lines=[("-", "b\n"), ("-", "c\n")])
    rows = G._rows_from_runs([dele], "a\nd\n", "f")
    assert [(r.old, r.new, r.type, r.text) for r in rows] == [
        (1, 1, "context", "a"), (2, None, "del", "b"),
        (3, None, "del", "c"), (4, 2, "context", "d")]

    # @@ -1,0 +2,2 @@ — new lines 2 and 3 added, after old line 1
    ins = G.Run(src_start=1, src_len=0, tgt_start=2, tgt_len=2,
                lines=[("+", "x\n"), ("+", "y\n")])
    rows = G._rows_from_runs([ins], "a\nx\ny\nb\nc\nd\n", "f")
    assert [(r.old, r.new, r.type, r.text) for r in rows] == [
        (1, 1, "context", "a"), (None, 2, "add", "x"), (None, 3, "add", "y"),
        (2, 4, "context", "b"), (3, 5, "context", "c"), (4, 6, "context", "d")]
    assert text  # the unchanged sides above are slices of it


def test_a_file_that_moved_under_us_is_an_error_not_a_bad_render():
    """Replaying a hunk past the end of the file must raise, not silently
    produce rows that don't match either side."""
    run = G.Run(src_start=10, src_len=1, tgt_start=10, tgt_len=1,
                lines=[("-", "old\n"), ("+", "new\n")])
    with pytest.raises(G.GitError, match="changed under us"):
        G._rows_from_runs([run], "just\ntwo\n", "f.py")
