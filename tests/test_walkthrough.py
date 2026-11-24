"""Validator tests (DEVELOPMENT.md, "Walkthroughs"). Run:
    .venv/bin/python -m unittest tests.test_walkthrough -v
The committed example doubles as the good-path fixture.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from seendiff import walkthrough as W

REPO = str(Path(__file__).resolve().parent.parent)
EXAMPLE = str(Path(REPO) / "examples" / "seendiff-review-state.json")


def make(tmp: str, mutate=None) -> str:
    """A minimal valid walkthrough against a tiny fake repo in tmp."""
    (Path(tmp) / "a.py").write_text("one\ntwo\nthree\nfour\n")
    data = {
        "version": 1,
        "title": "T",
        "acts": [{"id": "x", "title": "X"}, {"id": "y", "title": "Y"}],
        "steps": [
            {"id": "s1", "act": "x", "depth": 0, "title": "t", "text": "b",
             "ref": {"path": "a.py", "lines": [1, 2]}, "forward": "s2"},
            {"id": "s2", "act": "y", "depth": 1, "title": "t", "text": "b",
             "ref": {"path": "a.py", "lines": [3, 4]}},
        ],
    }
    if mutate:
        mutate(data)
    p = str(Path(tmp) / "wt.json")
    Path(p).write_text(json.dumps(data))
    return p


class Valid(unittest.TestCase):
    def test_example_loads(self):
        wt = W.load(EXAMPLE, REPO)
        self.assertEqual(wt.title, "How review state survives a rebase")
        self.assertGreaterEqual(len(wt.acts), 3)
        self.assertTrue(all(s.depth in (0, 1) for s in wt.steps))
        self.assertTrue(wt.digest and wt.mtime)
        self.assertIn("seendiff/store.py", wt.paths())

    def test_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = W.load(make(tmp), tmp)
            self.assertEqual([s.id for s in wt.steps], ["s1", "s2"])
            self.assertEqual(wt.steps[0].ref.lines, (1, 2))
            self.assertEqual(W.check(make(tmp), tmp), [])


class Empty(unittest.TestCase):
    """The placeholder walkthrough: seendiff opens on the plain diff and
    watches the file until the agent writes a script into it."""

    def each(self, fn):
        with tempfile.TemporaryDirectory() as tmp:
            p = str(Path(tmp) / "wt.json")
            for body in ("", "   \n", "{}", "null", '{"version": 1}',
                         '{"version": 1, "title": "T", "acts": [], "steps": []}'):
                Path(p).write_text(body)
                fn(p, tmp, body)

    def test_check_and_load(self):
        def check(p, tmp, body):
            self.assertEqual(W.check(p, tmp), [], body)
            self.assertIsNone(W.load(p, tmp), body)
        self.each(check)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = str(Path(tmp) / "never-written.json")
            self.assertEqual(W.check(p, tmp), [])
            self.assertIsNone(W.load(p, tmp))

    def test_half_written_json_is_still_an_error(self):
        # emptiness must not swallow a mid-flush write
        self.assertFalse(W.is_empty(b'{"version": 1, "steps": [{"id"'))


class Invalid(unittest.TestCase):
    def probs(self, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            return W.check(make(tmp, mutate), tmp)

    def assertProb(self, mutate, needle):
        probs = self.probs(mutate)
        self.assertTrue(any(needle in p for p in probs),
                        f"{needle!r} not in {probs}")

    def test_escape(self):
        self.assertProb(lambda d: d["steps"][0]["ref"].update(path="../pw"),
                        "outside the repository")

    def test_missing_file(self):
        self.assertProb(lambda d: d["steps"][0]["ref"].update(path="nope.py"),
                        "does not exist")

    def test_out_of_range(self):
        self.assertProb(lambda d: d["steps"][0]["ref"].update(lines=[3, 99]),
                        "out of range")

    def test_inverted(self):
        self.assertProb(lambda d: d["steps"][0]["ref"].update(lines=[3, 1]),
                        "a.py:3-1")

    def test_bad_depth(self):
        self.assertProb(lambda d: d["steps"][0].update(depth=2), "depth")

    def test_unknown_forward(self):
        self.assertProb(lambda d: d["steps"][0].update(forward="ghost"),
                        "names no step")

    def test_backward_forward(self):
        self.assertProb(lambda d: d["steps"][1].update(forward="s1"),
                        "later step")

    def test_interleaved_acts(self):
        def mut(d):
            d["steps"].append(dict(d["steps"][0], id="s3", forward=None))
        self.assertProb(mut, "interleaved")

    def test_duplicate_step_id(self):
        def mut(d):
            d["steps"][1]["id"] = "s1"
            d["steps"][0].pop("forward")
        self.assertProb(mut, "duplicate step id")

    def test_bad_version(self):
        self.assertProb(lambda d: d.update(version=2), "version must be 1")

    def test_too_many_also(self):
        def mut(d):
            d["steps"][0]["also"] = [{"path": "a.py", "lines": [1, 1]}] * 4
        self.assertProb(mut, "cap is 3")

    def test_step_cap(self):
        def mut(d):
            d["steps"][0].pop("forward")
            d["steps"] = [dict(d["steps"][0], id=f"s{i}") for i in range(41)]
        self.assertProb(mut, "cap is 40")

    def test_hunk_id_unknown(self):
        def mut(d):
            d["steps"][0]["ref"]["hunk_id"] = "deadbeef"
        with tempfile.TemporaryDirectory() as tmp:
            probs = W.check(make(tmp, mut), tmp, hunk_ids={"a.py": {"cafe"}})
            self.assertTrue(any("not in the diff" in p for p in probs), probs)

    def test_hunk_id_known(self):
        def mut(d):
            d["steps"][0]["ref"]["hunk_id"] = "cafe"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                W.check(make(tmp, mut), tmp, hunk_ids={"a.py": {"cafe"}}), [])

    def test_no_steps_is_empty_not_invalid(self):
        # a document with an empty steps list is the placeholder, not a bug
        with tempfile.TemporaryDirectory() as tmp:
            p = make(tmp, lambda d: d.update(steps=[]))
            self.assertEqual(W.check(p, tmp), [])
            self.assertIsNone(W.load(p, tmp))

    def test_broken_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = str(Path(tmp) / "bad.json")
            Path(p).write_text("{ nope")
            probs = W.check(p, tmp)
            self.assertTrue(any("invalid JSON" in x for x in probs), probs)
            with self.assertRaises(W.WalkthroughError):
                W.load(p, tmp)


if __name__ == "__main__":
    unittest.main()
