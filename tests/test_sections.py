"""Sectioning: an oversized block is cut into reviewable pieces (git.py)."""
from __future__ import annotations

import unittest

from seendiff import git as G


def run(lines, src_start=1, tgt_start=1):
    dels = sum(1 for t, _ in lines if t == "-")
    return G.Run(src_start=src_start, src_len=dels, tgt_start=tgt_start,
                 tgt_len=len(lines) - dels, lines=lines)


def plus(n, start=0):
    return [("+", f"    x = {i}\n") for i in range(start, start + n)]


class TestSections(unittest.TestCase):
    def test_small_block_untouched(self):
        b = G.Block(runs=[run(plus(20))])
        out = G.split_blocks([b], 12, 48)
        self.assertEqual(len(out), 1)
        self.assertIs(out[0], b)

    def test_sections_respect_min_and_max(self):
        out = G.split_blocks([G.Block(runs=[run(plus(500))])], 12, 48)
        sizes = [b.added + b.removed for b in out]
        self.assertEqual(sum(sizes), 500)
        self.assertTrue(all(s <= 48 for s in sizes), sizes)
        self.assertTrue(all(s >= 12 for s in sizes), sizes)

    def test_bounds_are_parameters(self):
        sizes = [b.added + b.removed
                 for b in G.split_blocks([G.Block(runs=[run(plus(300))])], 5, 10)]
        self.assertEqual(sum(sizes), 300)
        self.assertTrue(all(5 <= s <= 10 for s in sizes), sizes)

    def test_cuts_prefer_a_definition(self):
        lines = plus(60)
        lines[40] = ("+", "def alpha():\n")        # inside the [12, 48] window
        out = G.split_blocks([G.Block(runs=[run(lines)])], 12, 48)
        self.assertEqual([b.runs[0].lines[0][1] for b in out][1], "def alpha():\n")

    def test_line_numbers_partition_exactly(self):
        lines = [("-", f"old {i}\n") for i in range(60)] + plus(70)
        out = G.split_blocks([G.Block(runs=[run(lines, 10, 10)])], 12, 48)
        self.assertEqual(sum(r.src_len for b in out for r in b.runs), 60)
        self.assertEqual(sum(r.tgt_len for b in out for r in b.runs), 70)
        old = sorted(n for b in out for r in b.runs
                     for n in range(r.src_start, r.src_start + r.src_len))
        new = sorted(n for b in out for r in b.runs
                     for n in range(r.tgt_start, r.tgt_start + r.tgt_len))
        self.assertEqual(old, list(range(10, 70)))     # no gap, no overlap
        self.assertEqual(new, list(range(10, 80)))

    def test_splits_between_runs_before_cutting_one(self):
        """Three 20-line runs merged across small gaps: split at the seams git
        already gave us, not mid-run."""
        runs = [run(plus(20, s), src_start=s + 1, tgt_start=s + 1)
                for s in (0, 23, 46)]
        out = G.split_blocks([G.Block(runs=runs)], 12, 48)
        self.assertEqual([len(b.runs) for b in out], [2, 1])
        self.assertEqual([b.added for b in out], [40, 20])

    def test_ids_stay_content_derived(self):
        """Same content, different position — identical section ids, which is
        what lets review state survive a rebase."""
        a = G.split_blocks([G.Block(runs=[run(plus(120), 1, 1)])], 12, 48)
        b = G.split_blocks([G.Block(runs=[run(plus(120), 900, 900)])], 12, 48)
        G._assign_ids(a)
        G._assign_ids(b)
        self.assertEqual([x.hunk_id for x in a], [x.hunk_id for x in b])


if __name__ == "__main__":
    unittest.main()
