"""Run tests/marks.test.js under node, if node is around.

Search-match highlighting has to split pygments tokens at match boundaries —
the one piece of viewer JS with logic worth testing rather than eyeballing.
The runner pulls contentEl/appendTok straight out of index.html so the test
can't drift from the shipped code.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_search_mark_rendering():
    r = subprocess.run(["node", "tests/marks.test.js"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
