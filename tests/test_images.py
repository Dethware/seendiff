"""Image files: no text rows, before/after bytes from /api/blob."""
from __future__ import annotations

import struct
import subprocess
import zlib

import pytest
from fastapi.testclient import TestClient

from seendiff import git as G
from seendiff.server import AppState, create_app


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


OLD_LOGO, NEW_LOGO = png(64, 48, (200, 40, 40)), png(96, 48, (40, 160, 60))
GONE, ADDED = png(16, 16, (0, 0, 200)), png(32, 32, (240, 200, 0))
MOVED = png(8, 8, (1, 2, 3))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "logo.png").write_bytes(OLD_LOGO)
    (r / "gone.png").write_bytes(GONE)
    (r / "old-name.png").write_bytes(MOVED)
    (r / "data.bin").write_bytes(b"\0\1\2" * 100)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    (r / "logo.png").write_bytes(NEW_LOGO)
    (r / "added.png").write_bytes(ADDED)
    (r / "data.bin").write_bytes(b"\0\3\4" * 100)
    _git(r, "rm", "-q", "gone.png")
    _git(r, "mv", "old-name.png", "new-name.png")
    return r


@pytest.fixture
def client(repo, tmp_path):
    st = AppState(repo=str(repo), base_ref="HEAD",
                  db_path=str(tmp_path / "state.db"))
    with TestClient(create_app(st), headers={"host": "localhost"}) as c:
        yield c


def _file(client, path, mode="all"):
    r = client.get(f"/api/file?path={path}&mode={mode}")
    assert r.status_code == 200, r.text
    return r.json()


def test_images_have_no_rows_and_say_which_sides_exist(client):
    logo = _file(client, "logo.png")
    assert logo["lines"] == []
    assert logo["image"] == {"type": "image/png",
                             "old": len(OLD_LOGO), "new": len(NEW_LOGO)}
    assert _file(client, "added.png")["image"]["old"] is None
    assert _file(client, "gone.png")["image"]["new"] is None
    moved = _file(client, "new-name.png")
    assert moved["lines"] == [] and moved["image"]["old"] == len(MOVED)


def test_non_image_binaries_get_no_garbage_rows_and_no_image(client):
    d = _file(client, "data.bin")
    assert d["lines"] == [] and d["image"] is None


def test_blob_serves_each_side(client):
    def blob(path, side, mode="all"):
        return client.get(f"/api/blob?path={path}&side={side}&mode={mode}")
    r = blob("logo.png", "old")
    assert r.content == OLD_LOGO and r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers["content-security-policy"]
    assert blob("logo.png", "new").content == NEW_LOGO
    assert blob("new-name.png", "old").content == MOVED    # old path after a rename
    assert blob("added.png", "old").status_code == 404
    assert blob("gone.png", "new").status_code == 404
    assert blob("data.bin", "new").status_code == 404       # not an image type
    assert blob("logo.png", "sideways").status_code == 404


def test_blob_follows_the_mode(client, repo):
    _git(repo, "add", "logo.png")
    (repo / "logo.png").write_bytes(png(10, 10, (9, 9, 9)))
    staged = client.get("/api/blob?path=logo.png&side=new&mode=staged")
    assert staged.content == NEW_LOGO                        # the index copy
    client.get("/api/files?mode=unstaged")
    unstaged = client.get("/api/blob?path=logo.png&side=new&mode=unstaged")
    assert unstaged.content == png(10, 10, (9, 9, 9))        # the working tree
    assert client.get("/api/blob?path=logo.png&side=old&mode=unstaged").content == NEW_LOGO


def test_deleted_binary_keeps_its_sides_straight():
    fds = [fd for fd in G.parse_diff(
        "diff --git a/x.png b/x.png\ndeleted file mode 100644\n"
        "index 1111111..0000000\nBinary files a/x.png and /dev/null differ\n",
        "HEAD", G.WORKTREE)]
    assert fds[0].has_old and not fds[0].has_new
