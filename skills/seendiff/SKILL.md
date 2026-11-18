---
name: seendiff
description: >
  Show code in seendiff — a local diff viewer that tracks which blocks the
  user has reviewed, and can play a scripted, step-by-step explanation. Use
  when the user says "show me the diff", "let me review this", "explain this
  code / change / PR", "walk me through X", or otherwise asks to see or be
  guided through code in a git repository. After finishing a change
  (committed or not), offer it in one line.
---

# seendiff

Two jobs, one server:

1. **Show the diff.** Committed, staged and uncommitted work, in a browser
   tab that follows the working tree live.
2. **Explain code.** Write a walkthrough script into the file the running
   server is already watching; the open tab picks it up.

## What it shows

By default, **everything**: the branch's commits since the base, staged
changes, unstaged changes and untracked files, in one diff. The UI has
four tabs, switched with `m` / `M`:

| tab | compares | flag |
|---|---|---|
| all *(default)* | base → working tree | |
| commits | base → HEAD | `--committed` |
| staged | HEAD → index | `--staged` |
| unstaged | index → working tree, untracked included | `--unstaged` |

The base is `origin/master`, then `origin/main`, `master`, `main`, then
`HEAD` if none of those exist. If the user names one, pass it as the
positional argument. The tab polls every 2 s: edits, `git add` and new
commits show up without a reload. Review state is keyed on block
content, so a block reviewed under *unstaged* stays reviewed once it's
staged or committed.

Launch with the default view unless the user asks for a specific one.
They can switch tabs themselves.

## Prerequisite

`seendiff` must be on `PATH`. Check with `command -v seendiff`. If it's
missing, say so and stop. Installing software is the user's call, so
don't install it, and don't run it from some checkout's virtualenv.

## One server per working tree

Start at most one seendiff per working tree, and reuse it across
conversations. Each extra process holds a port and leaves a stale tab.

State lives in the repo's git dir. Use `git rev-parse --absolute-git-dir`,
never a literal `.git/`: in a linked worktree (Claude Code makes these),
`.git` is a file, not a directory.

- `$GITDIR/seendiff-port`: the port
- `$GITDIR/seendiff-walkthrough.json`: the script, `{}` when there's
  nothing to explain

**Launch procedure** (from anywhere inside the working tree):

```bash
command -v seendiff >/dev/null || echo MISSING
GITDIR=$(git rev-parse --absolute-git-dir)
ROOT=$(git rev-parse --show-toplevel)
WT="$GITDIR/seendiff-walkthrough.json"
PORT=$(cat "$GITDIR/seendiff-port" 2>/dev/null || shuf -i 8700-8999 -n 1)
echo "$PORT" > "$GITDIR/seendiff-port"
if curl -sf "http://127.0.0.1:$PORT/api/files" | grep -qF "\"repo\":\"$ROOT\""; then
  echo "ALREADY-RUNNING http://127.0.0.1:$PORT/"
else
  # keep a script that still matches the code (resume after a crash or the
  # next day); reset one that doesn't, or seendiff refuses to start
  seendiff --check-walkthrough "$WT" >/dev/null 2>&1 || echo '{}' > "$WT"
  echo "LAUNCH $PORT $WT"
fi
```

A walkthrough survives relaunches as long as it still validates, and the
reader comes back to the step they left (progress is stored per script
content). If the code has moved on so that its refs no longer resolve, the
check resets it to `{}` and the server opens as a plain diff. If you started
the server with a base ref, pass the same one to `--check-walkthrough`.

- `MISSING`: stop (see *Prerequisite*).
- `ALREADY-RUNNING`: **do not launch**. Give the user the URL.
- `LAUNCH`: start it as a **background** command. It serves until
  stopped, so a foreground run blocks the turn until it times out:

  ```bash
  seendiff --walkthrough "$WT" --port "$PORT"
  ```

  It opens the browser itself. Re-run the `curl` check a second later. If
  it still doesn't answer, read the background output. If the port was
  taken, delete `$GITDIR/seendiff-port` and go through the procedure again.

**Always pass `--walkthrough`**, even with nothing to explain. An empty
script (`{}`) is a supported state: seendiff opens as a plain diff and
watches the file, so a later explanation needs no relaunch and no new tab.

## Offering the diff

When you finish a change, offer it in one line, and don't launch unasked.
Usually that's before anything is committed: review comes first, and the
user may want to write the commit themselves. Don't commit just so there's
something to show; the default view includes uncommitted work.

> Done. Want to review it in seendiff?

If they say yes, run the launch procedure and give them the URL. Don't
paste a summary of the change into chat as well.

## Proposed commit messages

seendiff's commit panel (`c`, or the **commit** button) commits what's
staged, with a message the user picks and edits there. It reads proposals
from `COMMIT_MSG_<description>.txt` files in the repo root, one file per
queued change, so several sessions can each leave their own. To propose a
message, write such a file: a short subject line, plus `#` lines for notes
to the user, which are dropped on commit. Never commit for the user, and
never stage a COMMIT_MSG file. If the user has their own rules about
staging and commit messages, follow those. seendiff
leaves these files out of its diffs, and committing deletes the one used.
`git cm` does the same from a terminal.

## When the user asks you to explain something

You are **read-only with respect to the repo** here. You write exactly one
file, the walkthrough JSON, and never touch source files. Explaining code
is not a licence to fix it along the way. Refs are also checked once, when
the script loads: edit a file afterwards and its steps point at the wrong
lines.

1. **Read before scripting.** Read the files whole, not grep excerpts. Find
   the spine: the one piece of state or the one call path everything else
   hangs off. If there isn't one, the scope is too broad. Say so and
   propose a narrower walkthrough rather than writing 40 shallow steps.
2. **Outline acts first**, 3–6 of them, then fill in steps.
3. For a change walkthrough, get block ids from the server's own diff:
   `seendiff --list-blocks [base]` prints `path  hunk_id  lines  +a -r`
   for every block. Pass the same base the server was started with.
4. Write the JSON to `$WT`, the file the server is watching. Do not invent
   a new path.
5. **Run `seendiff --check-walkthrough "$WT" [base]`** with the same base,
   and fix every diagnostic. This is not optional: the file will not load
   with any problem outstanding.
6. Make sure the server is up (launch procedure), then give the user the
   URL and a two-line summary of the arc. An open tab picks the script up
   within about 2 s, with no reload. Don't paste the walkthrough prose into
   chat as well.
7. To revise, rewrite the file and re-run the validator. The server re-reads
   it live. Only relaunch if the process died.

The **dismiss** button in the walkthrough bar empties the JSON file, so
that's how the user ends a walkthrough for good. A script that's still in
the file when a new conversation starts may belong to earlier work: if the
user asks for something new, overwrite it rather than build on it.

Line refs are on the **working-tree** side, which is the new side of the
*all* and *unstaged* tabs. Under *commits* or *staged*, a file with
uncommitted edits has different line numbers, so play walkthroughs in the
default view.

## Default walkthrough structure

Unless the user asks for something else, use this order. Assume the reader
is a competent engineer with **zero knowledge of this domain or codebase**:
no jargon that hasn't been defined on screen, no assumed familiarity with
the product's vocabulary.

1. **Data structures first.** What the state is, what shape it has, what
   each field means. Naming what's being moved around makes the code that
   moves it readable.
2. **Then the main logic, driven by one concrete example.** Pick a real
   input (an actual file path, row or request) and walk *that example*
   through the code. "A block whose text hashes to `a3f9` …" beats "the
   block is hashed". For a change walkthrough, walk the example through the
   *new* path and say what it used to do instead.
3. **Then the corner cases.** Empty input, the concurrent write, the
   rebase, the failure branch. These come last and are `depth: 1`: someone
   reading only the main line should still get a complete, honest story.
4. **Simple language throughout.** Short sentences. Define a term the first
   time it appears. If a step needs a diagram to make sense, make it two
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

`{}` (or any document with no steps) is valid and means "no walkthrough".
That's what you launch with, and what the dismiss button writes back.

Rules the validator enforces:

- `lines` are 1-based, on the **working tree**, `[start, end]` inclusive,
  within the file, not inverted. Paths are repo-relative.
- Steps must be grouped in `acts[]` order, with no interleaving.
- `depth` is 0 (main line) or 1 (skippable detail), never 2. If a point
  needs three levels of nesting, it needs its own act.
- `also[]`: at most 3 secondary refs (definition site, caller, the test that
  pins it). They render as clickable chips and gutter anchors.
- `forward`: the id of a step that this one promises to come back to. It must
  exist and come *after* this step. Never leave a forward reference unpaid.
- Cap: 40 steps. Past that the scope was too big, and you should have said so.
- `ref.hunk_id` (optional, change walkthroughs only): a block id from
  `--list-blocks`. When present it wins over line numbers.

## Scripting rules: what separates a walkthrough from a narrated `cat`

- **Act 1 orients.** What this is, why it exists, what you'd lose
  without it. Two sentences, before any code.
- **One idea per step.** 40–90 words. If a step needs "and also", it's two
  steps.
- **Never paraphrase the code.** The reader can see it, syntax-highlighted
  and in context. Say what it's *for*, what the non-obvious constraint was,
  and what would break if it were written the obvious way.
- **Every non-obvious decision gets a why**, including the rejected
  alternative when it's knowable. "Content hash, not line numbers, so review
  state survives a rebase" is a walkthrough. "Computes a hash of the block"
  is not.
- **Main path in full before any edge case.** When an edge case can't be
  avoided early, name it in one clause and set `"forward"` to the step that
  pays it off.
- **Refs are tight.** The smallest range that carries the idea, 25 lines at
  most. Pointing at a whole 200-line function admits the step has no point.
  Use `also[]` for the definition site or the caller rather than widening.
- **Line numbers are read, not remembered.** They must come from a fresh
  read of the file in this session, not from memory or an earlier turn's
  output. Nothing corrects drift: a stale number is a load failure.
- **Close the loop.** The last step returns to the claim act 1 opened
  with, now that it's earned.
- For **change** walkthroughs: order by narrative, not by file or by
  commit. Use `hunk_id` refs. Say what the change makes possible, not what
  it edits.

## Anti-patterns: never do these

Enumerating files top to bottom. Restating signatures. "This function takes
a path and returns a list." Marketing adjectives. A step per commit.
Explaining language features rather than this code's decisions. Launching a
second seendiff process for the same working tree.
