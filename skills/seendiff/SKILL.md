---
name: seendiff
description: >
  Show code in seendiff — a local diff tool that can also play a scripted,
  step-by-step explanation. Use after making a commit (offer to show the diff),
  and when the user says "show me the diff", "explain this code / change / PR",
  "walk me through X", or otherwise asks for a guided explanation of code in a
  git repository.
---

# seendiff

Two jobs, one server:

1. **Show the diff.** After every commit you make, offer to open it in seendiff.
2. **Explain code.** When the user asks, write a walkthrough script into the
   file the running server is already watching — the open tab picks it up.

The server is launched *once per conversation*, always with an empty
walkthrough, so it starts as a plain diff tool and becomes a walkthrough
the moment you have something to explain.

## What must be installed

Two things, and the machine is set up once — not per repo, not per
conversation. Pick whatever installation method suits the machine; what
matters is that the end state below holds.

**1. The `seendiff` CLI.** A Python CLI (Python ≥ 3.10, plus `git`) from
`https://github.com/acme-dot-bot/seendiff`.

End state:

- `command -v seendiff` resolves from *any* directory, in a fresh shell, with
  no virtualenv activated and no path prefix typed.
- `seendiff --help` and `seendiff --check-walkthrough <file>` both run.
- It stays working after this repo's checkout is moved or deleted — no
  dependency on being run from inside the source tree.

**2. This skill.** The `seendiff` skill itself, installed for the user rather
than vendored into one project.

End state:

- The skill is available in every conversation, in any git repo, without
  copying files into that repo.
- `/seendiff` (or a natural request like "show me the diff") triggers it from
  anywhere, and it names the version installed here.
- Updating the copy in this repo does not silently change the installed
  skill — reinstalling is a deliberate step.

If either is missing you cannot do this skill's job: say so plainly, state
which of the two is absent, and give prose instead of writing a file nobody
can open.

## The one instance rule

One seendiff process per conversation, on one randomized port. Never start a
second one — the user ends up with orphaned servers and stale tabs, and every
extra process holds a port until it's killed.

State lives in two files under `.git/` (already ignored, and stable across
your author→fix loop):

- `.git/seendiff-port` — the port this conversation uses
- `.git/seendiff-walkthrough.json` — the script, `{}` when there's nothing to
  explain

**Launch procedure** (run from the repo root):

```bash
command -v seendiff || echo MISSING
PORT=$(cat .git/seendiff-port 2>/dev/null || shuf -i 8700-8999 -n 1)
echo "$PORT" > .git/seendiff-port
[ -s .git/seendiff-walkthrough.json ] || echo '{}' > .git/seendiff-walkthrough.json
curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && echo ALREADY-RUNNING
```

- `ALREADY-RUNNING` → **do not launch**. The user already has the tab; just
  give them `http://127.0.0.1:$PORT/`.
- Otherwise start it as a **background** Bash command — it serves until
  stopped, so a foreground run blocks the turn until timeout:

  ```bash
  seendiff --walkthrough .git/seendiff-walkthrough.json --port $PORT
  ```

  It opens the user's browser itself. If it exits immediately because the port
  is taken by something else, pick a new random port, rewrite
  `.git/seendiff-port`, and relaunch.
- If `command -v seendiff` prints `MISSING`, stop — see *What must be
  installed* above. Don't work around it with a path into some checkout's
  virtualenv; the prerequisite is a `seendiff` that runs from anywhere.

**Always pass `--walkthrough` with the empty file**, even when you have nothing
to explain. An empty walkthrough (`{}`) is a supported state: seendiff opens in
plain diff mode and watches the file, so a later explanation needs no relaunch
and no new tab.

By default seendiff diffs the branch against `origin/master`; pass a base ref
as a positional argument when the user names a different one.

## After a commit

Once you've committed, offer it in one line — don't launch unasked:

> Committed. Want to see the diff in seendiff?

If they say yes, run the launch procedure and hand over the URL. Nothing else:
no summary of the change pasted back into chat.

## When the user asks you to explain something

You are **read-only with respect to the repo** here. You write exactly one
file — the walkthrough JSON — and never touch source files. Explaining code is
not a license to "fix" it along the way, and seendiff's file cache assumes
files don't change under an open walkthrough.

1. **Read before scripting.** Read the files whole, not grep excerpts. Find the
   spine: the one piece of state or the one call path everything else hangs
   off. If there isn't one, the scope is too broad — say so and propose a
   narrower walkthrough rather than producing 40 shallow steps.
2. **Outline acts first**, 3–6 of them, then fill in steps.
3. Write the JSON to `.git/seendiff-walkthrough.json` — the same file the
   server is watching. Do not invent a new path.
4. **Run `seendiff --check-walkthrough .git/seendiff-walkthrough.json`** and fix
   every diagnostic. Non-negotiable: the file will not load with any problem
   outstanding.
5. Make sure the server is up (launch procedure above), then give the user
   `http://127.0.0.1:$PORT/` and a two-line summary of the arc. A tab that's
   already open picks the script up within ~2 s — no reload needed. Do **not**
   also paste the walkthrough prose into chat; that defeats the purpose.
6. Revisions: rewrite the file, re-run the validator. The server re-reads it
   live. Only relaunch if the process actually died.

The user can hit **dismiss** in the walkthrough bar to clear the script and go
back to the plain diff — that empties the JSON file. Treat the next explanation
as a fresh write, not a resume.

## Default walkthrough structure

Unless the user asks for something else, arrange it in this order. Assume the
reader is a competent engineer with **zero knowledge of this domain or
codebase** — no jargon that hasn't been defined on screen, no assumed
familiarity with the product's vocabulary.

1. **Data structures first.** What is the state, what shape is it, what does
   each field mean. Naming what's being moved around is what makes the code
   that moves it readable.
2. **Then the main logic, driven by one concrete example.** Pick a real input —
   an actual file path, an actual row, an actual request — and walk *that
   example* through the code. "A block whose text hashes to `a3f9` …" beats
   "the block is hashed". For a change walkthrough, walk the example through
   the *new* path and say what it used to do instead.
3. **Then the corner cases.** Empty input, the concurrent write, the rebase,
   the failure branch. These come last and are `depth: 1` — someone reading
   only the main line should still get a complete, honest story.
4. **Simple language throughout.** Short sentences. Define a term the first
   time it appears. If a step needs a diagram to make sense, it needs to be two
   steps instead.

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

`{}` (or any document with no steps) is valid and means "no walkthrough" —
that's what you launch with, and what the dismiss button writes back.

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
language features rather than this code's decisions. Launching a second
seendiff process.
