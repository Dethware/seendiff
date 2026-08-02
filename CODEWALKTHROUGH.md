# Code walkthroughs: an agent scripts it, seendiff drives it

Design doc. Nothing here is implemented yet.

## Goal

Two halves that only make sense together:

1. **A skill** (`code-diff`) that turns "explain this code" / "explain this
   change" into a *scripted* walkthrough — an ordered list of code references with
   layered prose — written to a JSON file.
2. **A seendiff mode** that plays that file back: a bottom bar under the code pane
   carrying the current explanation, with `←` / `→` rebound to step through the
   script instead of through change blocks.

The point is the layering. A dump of "here's what each function does" is a worse
version of reading the code. A walkthrough earns its length by *ordering*: the
shape of the data before the code that fills it, the main path before the edge
cases, a forward reference where an edge case can't be avoided ("that's what the
`deleted` flag is for — more on that later"). That ordering is the skill's real
output; the JSON is just how it gets shipped.

seendiff is the right host because it already has the hard parts: whole-file
rendering, per-line highlighting, exact scroll geometry, a minimap, and a
keyboard-first UI. A walkthrough is ~40 lines of new UI on top of that.

---

## Locked decisions

| Decision | Value | Rationale |
|---|---|---|
| Transport | A JSON file passed on the CLI | No socket, no agent dependency in seendiff. The skill writes, seendiff reads. Keeps CHAT-SUPPORT.md's agent integration entirely orthogonal |
| Ref granularity | `path` + 1-based line range on the **working tree / new side** | Same numbering as `y` refs and the gutter, so a walkthrough ref and a copied ref are the same string |
| Drift handling | **None** | A walkthrough is written against the current worktree and read minutes later. A ref that doesn't resolve is a load error, caught by the validator before the user ever sees it — regenerate, don't reconcile |
| Non-diff files | Supported — served as an all-context whole file. Any path in the repo is openable while a walkthrough is loaded | "Explain this code" usually has no diff at all. Restricting walkthroughs to changed files would gut the feature |
| Empty diff | Walkthrough mode still starts | Same reason |
| Layering | Two axes: **acts** (coarse, ordered, authored) and **depth** (per-step detail, skippable) | Acts carry "data → population → wiring → edge cases". Depth carries "you can skip this one" |
| Reviewing | Never automatic. Finishing a walkthrough marks nothing; the user marks blocks as they go with the existing keys | A walkthrough is an explanation, not a sign-off. Coupling them would put review state one keystroke from being wrong |
| Progress state | `localStorage`, keyed by a hash of the walkthrough | A walkthrough is ephemeral; it does not belong in `state.db` alongside review state that must survive a rebase |
| Live reload | Always on, 2 s mtime poll | Makes the agent's author→validate→look loop tight. Rides the interval that already exists at `index.html:955` |
| Validation | Fail loudly at load, plus a `--check-walkthrough` mode | The input is machine-generated; silent skipping of a bad ref would let the agent ship a broken script |

---

## 1. The file format

```json
{
  "version": 1,
  "title": "How review state survives a rebase",
  "intro": "Two sentences on what this covers and why it's arranged this way.",
  "subject": { "kind": "code", "repo_head": "dc2e41c" },
  "acts": [
    { "id": "data",  "title": "The state is three columns and a scope string" },
    { "id": "write", "title": "Seen is passive, reviewed is deliberate" },
    { "id": "edges", "title": "What breaks it" }
  ],
  "steps": [
    {
      "id": "row-shape",
      "act": "data",
      "depth": 0,
      "title": "One row per (scope, path, hunk_id)",
      "text": "Everything seendiff remembers is this one table...",
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

Field notes:

- `act` — id into `acts[]`. Steps must appear in act order; the loader rejects
  interleaving, because the pip strip and the act-jump keys assume contiguity.
- `depth` — `0` is the main line, `1` is a detail or edge case hanging off the
  preceding depth-0 step. Depth ≥ 2 is rejected; if a point needs three levels of
  nesting, it needs its own act.
- `ref` — the primary target. The pane scrolls here and the lines get the
  walkthrough highlight.
- `also[]` — secondary targets shown as clickable chips in the bar (definition
  site, caller, the test that pins it). Clicking one jumps without advancing the
  step; `→` from there returns to the primary ref. No more than three. Each
  `also` ref also renders as an **anchor** in the code itself — see §3.
- `forward` — a step id this one promises to come back to. Renders as a
  "→ later: *Hunk-id collisions*" affordance, and the loader errors if the target
  doesn't exist or isn't *after* this step. This is the mechanism that makes
  "more on that later" structural rather than a phrase the model may or may not
  honour.
- `hunk_id` on a ref (optional) — for change-walkthroughs. When present it wins
  over line numbers, and survives a rebase exactly like review state does.

Cap: 40 steps. Past that the scope was too big and the skill should have said so.

## 2. Server: `seendiff/walkthrough.py` (new, ~130 lines)

Pure loading, validation and ref resolution. No FastAPI.

```python
@dataclass
class Ref:  path: str; lines: tuple[int, int]; hunk_id: str | None; note: str | None
@dataclass
class Step: id; act; depth; title; text; ref: Ref; also: list[Ref]; forward: str | None
@dataclass
class Walkthrough: title; intro; acts; steps; path; mtime; digest
```

- `load(path, repo) -> Walkthrough` — parse, schema-check, check every ref.
- `check_ref(ref, repo)` — two conditions, both hard errors:
  1. Path resolves inside the repo (`Path.resolve().relative_to(repo)`) and
     exists. This is a file-read primitive reachable from a JSON file, so the
     containment check is load-bearing, not hygiene.
  2. Line range is within the file and non-inverted.
  3. When `hunk_id` is present, it names a real block of that file's diff —
     resolved through the same `block_meta` the client gets.
- `check(path, repo) -> list[str]` — the same checks, collected as
  human-readable diagnostics for the validator instead of raised.

Diagnostics name the JSON path and the reason, e.g.
`walkthrough.json: steps[3].ref → seendiff/store.py:200-210 (file has 108 lines)`.

### `seendiff/git.py` (+~35 lines)

`worktree_rows(repo, path) -> list[Row]` — read a tracked file from the working
tree, emit one `Row` per line with `type="ctx"`, `old == new == i + 1`, no
`hunk_id`. Size and binary guards mirror `highlight.guard`.

### `seendiff/server.py` (+~90 lines)

- `AppState` gains `wt: Walkthrough | None` and `wt_paths: set[str]` (the
  walkthrough's files that aren't in the diff).
- `GET /api/walkthrough` — the resolved script plus `mtime` and `digest`. 404
  when no walkthrough is loaded.
- `GET /api/file?path=…` — currently 404s on anything not in `st.by_path`
  (`server.py:252`). Extend: if the path is in `wt_paths`, build a payload from
  `worktree_rows` with `blocks: []`. Same shape as today, so the client's
  virtual scroller, highlighter and geometry code need no branch. Worktree
  payloads are **not** disk-cached and the in-memory cache is keyed by
  `(path, mtime)`: the diff cache's `mb-head` key only invalidates when HEAD
  moves (`server.py:90`), which is correct for immutable diff payloads but
  would serve stale content for a worktree file edited in place. The skill is
  read-only (§4), so in practice this only fires when the *user* edits a file —
  but a rebuild is just read + highlight, so caching conservatively costs
  nothing.
- `POST /api/seen` and `/api/reviewed` keep rejecting unknown paths — a
  walkthrough-only file has no blocks and no review state, by construction.
- Reload-on-mtime-change happens in the `/api/walkthrough` handler, next to the
  existing `maybe_refresh` pattern. A reload that fails validation returns the
  previous good copy plus an `error` field, which the bar shows in place of the
  prose. Never leave the user staring at a blank bar because the agent wrote a
  half-flushed file.

### `seendiff/cli.py` (+~25 lines)

```
seendiff --walkthrough PATH          # open in walkthrough mode
seendiff --check-walkthrough PATH    # validate, print diagnostics, exit 0/1
```

`--walkthrough` implies "don't bail on an empty diff". Two degradations to
handle explicitly, since "explain this code" often runs on a clean checkout:

- No committed changes vs the base → today `init()` prints "nothing to review"
  and returns (`index.html:1247`). With a walkthrough loaded, skip that early
  return and open the first step's file.
- `resolve_base` raises (no `origin/master`, fresh repo) → today that's a fatal
  `return 1` (`cli.py:44`). With `--walkthrough`, warn and run with an empty
  diff. A walkthrough of existing code doesn't need a base ref at all.

## 3. Frontend: `static/index.html` (+~260 lines)

### The bar

A `flex: none` child of `#main`, after `#pane-wrap`, so the pane shrinks
naturally and the sidebar is untouched. Full width of the code area including
the minimap.

```
┌─ sidebar ─┬─ topbar ─────────────────────────────────────────────┐
│           │ pane                                    │ minimap    │
│           ├──────────────────────────────────────────────────────┤
│           │ THE DATA · 4/17          One row per (scope, path…)  │
│           │ Everything seendiff remembers is this one table. The │
│           │ scope string is the interesting column — see step 9. │
│           │ [store.py:12-22] [+ server.py:82]   → later: collisions│
│           │ ●●●○○○ │ ○○○○○ │ ○○○○○○            ← → step · w exit │
└───────────┴──────────────────────────────────────────────────────┘
```

- Height from a `--wt-height` CSS var, default 160 px, drag handle on the top
  edge reusing the `#sb-resizer` pattern (`index.html:1062`). Prose overflows to
  a scroll inside the bar; it never grows the bar.
- Pip strip: one pip per step, gaps between acts, current one filled, visited
  ones outlined, depth-1 pips half-size. Clickable → jump to step.
- Depth-1 steps render with a left inset and a `detail` tag so it's visible that
  the main line is being left temporarily.
- No stale/moved states to render — a walkthrough that doesn't resolve never
  loads. On a live reload that fails validation, the bar shows the error text
  and keeps the previous good script (§2).

### State and navigation

```js
let wt = null, wtStep = -1, wtOn = false, wtDetail = true, wtVisited = new Set();
```

- `wtSeq()` — the step indices currently in play: all steps when `wtDetail`,
  else depth-0 only. `←`/`→` walk this array, so toggling detail off makes the
  main line a clean spine.
- `gotoStep(i)`:
  1. `if (step.ref.path !== curPath) await openFile(step.ref.path)`
  2. resolve the ref's new-side line numbers to line indices first —
     `file.lines.findIndex(l => l.new === n)` — *then* map those through
     `viewOf[]` to view rows. `viewOf` is indexed by position in `file.lines`,
     not by new-side number; the two coincide only in walkthrough-only files.
     A new-side number inside a deleted region has no line, but line refs are
     defined on the new side so that's unreachable except via `hunk_id` refs,
     which resolve through `block_meta` instead and sidestep it
  3. scroll so the range top sits at ¼ pane height, or centre it if it fits
  4. set `wtHi = {r0, r1}`, `renderRows()`, `drawMinimap()`
  5. update the bar, push `wtVisited`, save progress, update the hash
- `renderRows` (`index.html:737`) gains one class: `.row.wt-hi` — accent-tinted
  background plus a 2 px left ring, a step up in weight from `.row.selected` so
  it reads as "this is the subject" not "this is selected". In split mode both
  halves get it.
- `drawMinimap` (`index.html:836`) gains an accent bracket at the current ref.

### Anchors

The agent's way of saying "I've marked where this happens — go look" without
making it the step's subject. While a step is active, each of its `also[]`
refs whose path is the open file gets a small accent dot in the gutter at the
ref's first line (a `::before` on the `.row.wt-anchor` line-number cell —
subtle: ~6 px, half-opacity, no glow) and a thin tick in the minimap. Clicking
the dot behaves exactly like clicking the chip: jump there, step unchanged,
`→` returns to the primary ref. Cross-file `also` refs are chips only until
that file is opened — then the anchor is waiting in the gutter.

Anchors belong to the *current step* only; they clear on step change. Marking
every step's anchors at once would turn the gutter into a constellation and
bury the one signal that matters. The ≤3 cap on `also[]` keeps the worst case
quiet. Implementation is a row-index set recomputed in `gotoStep`, one class
toggle in `renderRows`, one loop in `drawMinimap` — ~25 lines inside the
phase-3/4 budget.
- The bar's height change means `pane.clientHeight` moves: `setWalkthrough(on)`
  must call `computeOffsets(); renderRows(); drawMinimap()`, the same trio
  `setSplit` already runs (`index.html:1109`).

### Keymap

Rebinding arrows is the whole ask, so it needs a visible mode and a cheap way
out. Walkthrough mode is **on by default when `--walkthrough` is passed**.

| Key | Normal | Walkthrough mode |
|---|---|---|
| `←` `→` | prev/next block | **prev/next step** |
| `[` `]` | — (unbound, stays free) | prev/next **act** |
| `.` | — | toggle detail (depth-1) steps |
| `w` | open walkthrough bar if one is loaded | exit walkthrough mode (arrows revert) |
| `Shift+←` `Shift+→` | — | prev/next block — the escape hatch, unchanged behaviour |
| `↑` `↓` | prev/next file | unchanged |
| `n` `N` `R` `V` `f` `y` `Y` | unchanged | unchanged |
| `1`–`9` | — | jump to act *n* |

The help overlay (`index.html:258`) gets a walkthrough section that only renders
when a walkthrough is loaded. `?` and `Esc` behave as they do today.

Everything else — seen tracking, reviewed state, `y` refs — keeps working while
a walkthrough plays. A walkthrough of a change *is* a review pass; blocks the
walkthrough scrolls through get marked seen by the existing dwell logic, which
is exactly right.

Reviewing is never automatic — finishing a walkthrough marks nothing. The user
marks blocks as they go, with the keys they already know. Two adjustments make
that work:

- `gotoStep` sets `focusIdx` from `blockByRow[r0]` when the step's ref lands
  inside a change block, so `R` / `V` / `y` act on the code the bar is talking
  about rather than on wherever the focus happened to be left. When the ref is
  in a walkthrough-only file, or in unchanged code, there is no block —
  `focusIdx = -1` and `R` toasts "no change block here" instead of acting on a
  stale index.
- `R` in walkthrough mode toggles reviewed and **stays put**. Today it chases
  `nextUnreviewed(1)` (`index.html:1193`), which would yank the pane off the
  step mid-sentence. The advance belongs to `→` while a walkthrough is playing.

### Sidebar

A collapsible **Walkthrough** section above the file filter: act titles, each
expanding to step titles, current one marked, all clickable. Files that exist
only in the walkthrough are appended to the file list under a `walkthrough`
heading with a distinct glyph, so it's obvious they aren't part of the diff.

### Deep links and progress

Hash becomes `#f=…&l=…&w=<step-id>`. The scroll handler currently rebuilds the
hash from scratch with only `f` and `l` on every scroll frame
(`index.html:1226`) — it must learn to carry `w` through, or the deep link dies
on the first scroll. Progress (`{digest, stepId, visited}`) goes
to `localStorage` under `seendiff.wt.<digest>`; a walkthrough that changes
content gets a new digest and therefore a clean slate, which is what you want
when the agent rewrites it.

## 4. The skill

Lives at `skills/code-diff/SKILL.md` in this repo, symlinked into
`~/.claude/skills/` — it's used from *other* repos, so a project-level skill
here would never fire where it's needed. README gets the one-line install.

Frontmatter description covers the triggers: "explain this code", "walk me
through this change / PR / file", "how does X work".

### Procedure the skill mandates

0. **The skill is read-only with respect to the repo.** It writes exactly one
   file — the walkthrough JSON under `.git/` — and never touches source files.
   Explaining code is not a license to "fix" it along the way, and the server's
   worktree-file cache assumes files don't change under an open walkthrough (§2).
1. **Check the tool exists** — `command -v seendiff`. If absent, say so and give
   prose instead; don't write a JSON file nobody can open.
2. **Read before scripting.** Read the files whole, not grep excerpts. Find the
   spine: the one piece of state or the one call path everything else hangs off.
   If there isn't one, the scope is too broad — say so and propose a narrower
   walkthrough rather than producing 40 shallow steps.
3. **Outline acts first**, 3–6 of them, then fill steps.
4. Write to `.git/seendiff-walkthrough.json` (inside the repo, git-ignored by
   virtue of being under `.git/`, and stable across the author→fix loop).
5. **Run `seendiff --check-walkthrough`** and fix every diagnostic. Non-negotiable.
6. Hand the user the exact command and a two-line summary of the arc. Do not
   also paste the walkthrough prose into chat — that defeats the purpose.

### Scripting rules (the part that matters)

These go in the skill verbatim, because they're what separates a walkthrough
from a narrated `cat`:

- **Act 1 orients.** What is this, why does it exist, what would you lose
  without it. Two sentences, before any code.
- **Data before behaviour.** Name the state and its shape first — the struct,
  the table, the JSON payload. Then who writes it. Then who reads it. Then what
  breaks it. Anyone can follow code once they know what's being moved around.
- **One idea per step.** 40–90 words. If a step needs "and also", it's two steps.
- **Never paraphrase the code.** The reader can see it — it's on screen, syntax
  highlighted, in context. Say what it's *for*, what the non-obvious constraint
  was, what would break if it were written the obvious way instead.
- **Every non-obvious decision gets a why**, including the rejected alternative
  when it's knowable. "Content hash, not line numbers — so review state survives
  a rebase" is a walkthrough. "Computes a hash of the block" is not.
- **Main path in full before any edge case.** When an edge case can't be avoided
  early, name it in one clause and set `"forward"` to the step that pays it off.
  Never leave a forward reference unpaid — the loader enforces this.
- **Edge cases, failure modes and perf hacks are `depth: 1`** or a final act.
  Someone skimming with detail off should still get a complete, honest story.
- **Refs are tight.** The smallest range that carries the idea, ≤ 25 lines.
  Pointing at a whole 200-line function is an admission the step has no point.
  Use `also[]` for the definition site or the caller rather than widening.
- **Line numbers are read, not remembered.** They must come from a fresh read of
  the file in this session, not from memory or from an earlier turn's output.
  There is no drift correction: a stale number is a load failure.
- **Close the loop.** The last step returns to the claim act 1 opened with, now
  that it's earned.
- For **change** walkthroughs: order by narrative, not by file or by commit. Use
  `hunk_id` refs. Say what the change makes possible, not what it edits.

### Anti-patterns the skill names explicitly

Enumerating files top to bottom. Restating signatures. "This function takes a
path and returns a list." Marketing adjectives. A step per commit. Explaining
language features rather than this code's decisions.

## 5. Build order

| Phase | Work | ~LOC | Independently useful? |
|---|---|---|---|
| 1 | `walkthrough.py` + `--check-walkthrough` + tests against hand-written JSON | 160 | Yes — validator is the skill's contract |
| 2 | `worktree_rows`, `/api/file` fallback, `/api/walkthrough`, `--walkthrough`, empty-diff degradations | 130 | Yes — seendiff can open arbitrary repo files |
| 3 | Bar markup/CSS, `gotoStep`, highlight, keymap, help | 200 | The feature |
| 4 | Sidebar outline, pips, `also[]` chips, forward-refs, live reload | 80 | Polish |
| 5 | `SKILL.md` + a worked example walkthrough of seendiff itself | 200 | Doubles as the demo and the test fixture |

Phase 5's example — a walkthrough of seendiff's own rebase-survival design —
should be committed as `examples/seendiff-review-state.json` and used as the
validator's fixture. It's also the honest test of whether the format can carry a
good explanation.

## 6. Out of scope

Authoring or editing walkthroughs in the UI. Recording/playback timing or
autoplay. Audio. Exporting to Markdown or HTML. Multi-repo walkthroughs.
Walkthroughs that reference commits other than the current worktree state.
Quizzes. Any coupling to the CHAT-SUPPORT.md agent — a walkthrough is a static
artifact, and keeping it that way is what makes it reviewable, diffable, and
shareable as a file.

## 7. Open questions

1. **`w` to exit, or `Esc`?** `Esc` is currently the universal close, but it's
   also how you dismiss help/confirm; overloading it to also drop out of
   walkthrough mode risks exiting when the user meant to close a dialog. Leaning
   `w` only.
2. **Detail-off by default?** Starting with the clean spine and letting the user
   press `.` for more is arguably the better first impression, but a walkthrough
   that silently hides a third of its steps may read as thin. Leaning detail-on.

## Addendum: the empty walkthrough (implemented after the fact)

The skill became a *diff* skill: it launches one seendiff per conversation, on
one randomized port, and always passes `--walkthrough
.git/seendiff-walkthrough.json` — even when there is nothing to explain yet. So
"no script" had to stop being a load failure and become a state.

- `walkthrough.is_empty()` decides it: file missing, whitespace-only, `{}`,
  `null`, or a document with no `steps`. `load()` returns `None` for those and
  `check()` reports no problems; half-written JSON stays an error, so a
  mid-flush read never reads as "dismissed".
- `AppState.wt_empty` separates "empty, keep watching" from "broken, never
  loaded". `GET /api/walkthrough` answers `{"empty": true}` (200) for the first
  and 422 for the second; 404 still means the server was started without a
  walkthrough file at all.
- The client polls on `wtWatch` (a file is configured) rather than on `wt` (a
  script is loaded), so a script written into the file while the tab is open is
  adopted live — it enters walkthrough mode and toasts the title.
- **Dismiss** in the bar `POST`s `/api/walkthrough/dismiss`, which truncates the
  file to `{}`. Truncating rather than hiding is what makes it stick: the agent
  owns that file, and every other open tab drops the walkthrough on its next
  poll. `w` still just hides the bar.
