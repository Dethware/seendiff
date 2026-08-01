---
name: code-walkthrough
description: >
  Script a guided, step-by-step code walkthrough and play it back in seendiff.
  Use when the user says "explain this code", "walk me through this change /
  PR / file", "how does X work", or otherwise asks for a guided explanation of
  code in a git repository.
---

# code-walkthrough

Turn "explain this code" into a *scripted* walkthrough: an ordered list of code
references with layered prose, written to a JSON file that `seendiff
--walkthrough` plays back. Your real output is the *ordering* — the shape of
the data before the code that fills it, the main path before the edge cases. A
dump of "here's what each function does" is a worse version of reading the
code.

## Procedure

0. **You are read-only with respect to the repo.** You write exactly one file —
   the walkthrough JSON under `.git/` — and never touch source files.
   Explaining code is not a license to "fix" it along the way, and seendiff's
   worktree-file cache assumes files don't change under an open walkthrough.
1. **Check the tool exists**: `command -v seendiff`. If absent, say so and give
   prose instead; don't write a JSON file nobody can open.
2. **Read before scripting.** Read the files whole, not grep excerpts. Find the
   spine: the one piece of state or the one call path everything else hangs
   off. If there isn't one, the scope is too broad — say so and propose a
   narrower walkthrough rather than producing 40 shallow steps.
3. **Outline acts first**, 3–6 of them, then fill in steps.
4. Write the JSON to `.git/seendiff-walkthrough.json` (inside the repo,
   git-ignored by virtue of being under `.git/`, stable across your
   author→fix loop).
5. **Run `seendiff --check-walkthrough .git/seendiff-walkthrough.json`** and
   fix every diagnostic. Non-negotiable — the file will not load with any
   problem outstanding.
6. Hand the user the exact command
   (`seendiff --walkthrough .git/seendiff-walkthrough.json`) and a two-line
   summary of the arc. Do **not** also paste the walkthrough prose into chat —
   that defeats the purpose.

## File format

```json
{
  "version": 1,
  "title": "How review state survives a rebase",
  "intro": "Two sentences on what this covers and why it's arranged this way.",
  "acts": [
    { "id": "data", "title": "The state is three columns and a scope string" }
  ],
  "steps": [
    {
      "id": "row-shape",
      "act": "data",
      "depth": 0,
      "title": "One row per (scope, path, hunk_id)",
      "text": "40–90 words of prose for the bar.",
      "ref": { "path": "seendiff/store.py", "lines": [12, 22] },
      "also": [
        { "path": "seendiff/server.py", "lines": [82, 83],
          "note": "where the scope string is built" }
      ],
      "forward": "hunk-id-collision"
    }
  ]
}
```

Rules the validator enforces:

- `lines` are 1-based, on the **working tree / new side**, `[start, end]`
  inclusive, within the file, not inverted. Paths are repo-relative.
- Steps must be grouped in `acts[]` order — no interleaving.
- `depth` is 0 (main line) or 1 (skippable detail). Never 2 — if a point needs
  three levels of nesting, it needs its own act.
- `also[]`: at most 3 secondary refs (definition site, caller, the pinning
  test). They render as clickable chips and gutter anchors.
- `forward`: a step id this step promises to come back to; must exist and be
  *after* this step. Never leave a forward reference unpaid.
- Cap: 40 steps. Past that the scope was too big and you should have said so.
- `ref.hunk_id` (optional, change-walkthroughs only): a seendiff block id; when
  present it wins over line numbers and survives a rebase like review state.

## Scripting rules — what separates a walkthrough from a narrated `cat`

- **Act 1 orients.** What is this, why does it exist, what would you lose
  without it. Two sentences, before any code.
- **Data before behaviour.** Name the state and its shape first — the struct,
  the table, the JSON payload. Then who writes it. Then who reads it. Then
  what breaks it. Anyone can follow code once they know what's being moved
  around.
- **One idea per step.** 40–90 words. If a step needs "and also", it's two
  steps.
- **Never paraphrase the code.** The reader can see it — it's on screen, syntax
  highlighted, in context. Say what it's *for*, what the non-obvious
  constraint was, what would break if it were written the obvious way instead.
- **Every non-obvious decision gets a why**, including the rejected alternative
  when it's knowable. "Content hash, not line numbers — so review state
  survives a rebase" is a walkthrough. "Computes a hash of the block" is not.
- **Main path in full before any edge case.** When an edge case can't be
  avoided early, name it in one clause and set `"forward"` to the step that
  pays it off.
- **Edge cases, failure modes and perf hacks are `depth: 1`** or a final act.
  Someone skimming with detail off should still get a complete, honest story.
- **Refs are tight.** The smallest range that carries the idea, ≤ 25 lines.
  Pointing at a whole 200-line function is an admission the step has no point.
  Use `also[]` for the definition site or the caller rather than widening.
- **Line numbers are read, not remembered.** They must come from a fresh read
  of the file in this session, not from memory or an earlier turn's output.
  There is no drift correction: a stale number is a load failure.
- **Close the loop.** The last step returns to the claim act 1 opened with,
  now that it's earned.
- For **change** walkthroughs: order by narrative, not by file or by commit.
  Use `hunk_id` refs. Say what the change makes possible, not what it edits.

## Anti-patterns — never do these

Enumerating files top to bottom. Restating signatures. "This function takes a
path and returns a list." Marketing adjectives. A step per commit. Explaining
language features rather than this code's decisions.
