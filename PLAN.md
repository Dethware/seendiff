# seendiff — implementation spec

A local, point-in-time diff tool for stacked commits. Renders the full diff of a
branch against `origin/master`, shows changed files whole (not as hunk fragments), tracks
which changes you've looked at and which you've explicitly signed off, and makes it easy
to copy precise line references for pasting into an LLM.

Status: implemented (v0.1). This document was the build brief; deviations are noted
inline. Run with `.venv/bin/python -m seendiff.cli` from any repo, or `pip install -e .`
and use `seendiff`.

---

## 1. Motivation

Reviewing your own stack of commits before pushing is badly served by existing tools:

- **Meld, kdiff3, diffuse, xxdiff, Beyond Compare** are all stateless. Close the window
  and your progress is gone. There is no "mark as seen" in any of them.
- **`git difftool`** spawns one process per file pair — fine for merging, useless for
  reviewing 20 files as a unit.
- **Forge web UIs** (GitHub, GitLab, Gitea, Forgejo) have exactly the viewed-checkbox
  behaviour we want, but require pushing a branch and opening a PR to get it, and show
  hunks-with-3-lines-of-context rather than whole files.
- **`git range-diff`** answers "what changed since my last review pass" but is a
  read-once terminal command with no persistent state.

seendiff is a small local tool that fills that gap: forge-style review state, whole-file
context, no server, no PR.

**Explicitly out of scope:** editing, merging, conflict resolution, comments, file
watching, live reload, multi-repo dashboards. It is a reader, not an editor. If you need
to change something, note the line ref and use your editor.

---

## 2. Locked decisions

Each of these was considered and settled; they are inputs to the build, not open
questions.

| Decision | Value | Rationale |
|---|---|---|
| Diff scope | Committed only | Reproducible; makes the diff immutable, so everything caches permanently |
| Base | `origin/master` → `origin/main` → local `master`/`main` | Resolve with `rev-parse --verify`, don't guess from config |
| Merge base | `git merge-base` once, diff against that SHA | Three-dot semantics; two-dot folds in upstream work as reversed hunks. Explicit call, not `diff --merge-base` — the SHA is needed anyway (header, cache key, detached-HEAD fallback) and it avoids requiring git ≥ 2.30 |
| Layout | Whole file, unified | Line refs need real file numbering; side-by-side has no horizontal budget left after minimap |
| Review states | seen (automatic) + reviewed (explicit) | Two distinct signals; see §6 |
| Scope key | Symbolic, includes branch name | Survives rebase — the single most important design decision, see §5 |
| Block identity | Content hash of a block's changed lines | Position-independent; auto-invalidation falls out free; blocks self-grouped from `-U0`, see §4 |
| Highlighting | Server-side Pygments, token stream | Client libraries emit spans crossing line boundaries |
| Frontend | Single HTML file, no build step | Personal utility; an npm toolchain is a liability |
| Fetching | Never implicit | A review tool must not mutate refs; `--fetch` is opt-in |

Two minor calls taken by default, cheap to revisit:

- **Deleted-line refs** emit the *preceding new-side* line number, with the toast reading
  `src/pipeline.py:41 (deleted lines)`, and the deleted content included in the copy
  payload. The alternative — old-side numbering — is wrong against the working tree the
  LLM will be reasoning about.
- **Ref basket** (accumulate refs across several files, copy as a set) is deferred. ~40
  lines to add later; see §14.

---

## 3. Stack

| Layer | Choice | Notes |
|---|---|---|
| Diff source | `git` subprocess | Not `GIT_EXTERNAL_DIFF` or difftool — see §4 |
| Diff parsing | `unidiff` (PyPI) | Don't hand-roll a unified-diff parser |
| Highlighting | `pygments` | Token stream, not its HTML formatter |
| Server | `fastapi` + `uvicorn` | Flask is equivalent here; difficulty isn't in this layer |
| State | `sqlite3` (stdlib) | No ORM, no migrations |
| Frontend | One `index.html`, vanilla JS | Alpine.js from CDN if you want reactive bindings |

State at `~/.local/state/seendiff/state.db`, cache at `~/.cache/seendiff/` (XDG).

### Layout

```
seendiff/
  seendiff/__init__.py
  seendiff/cli.py          # argparse, free port, webbrowser.open, uvicorn.run
  seendiff/git.py          # ref resolution, diff invocation, parsing
  seendiff/highlight.py    # Pygments token-stream tokenizer
  seendiff/store.py        # sqlite open / upsert / delete / prune
  seendiff/server.py       # FastAPI app + routes
  seendiff/static/index.html
  seendiff/static/fonts/   # UbuntuSansMono, JetBrainsMono woff2 (both OFL — vendor them)
  pyproject.toml           # deps: fastapi, uvicorn, unidiff, pygments
```

### CLI

```
seendiff [BASE] [--fetch] [--clear] [--no-auto-seen] [--no-highlight]
         [--port N] [--no-browser]
```

`BASE` defaults to `origin/master`. Starts uvicorn on a free port, opens a browser.
Feels like a CLI tool, is actually a local web app.

---

## 4. Git integration

Git is a subprocess we call, never a program that calls us. `git difftool` and
`GIT_EXTERNAL_DIFF` both invert control — they invoke your binary once per file pair with
temp paths as argv, which cannot drive a persistent UI.

### Primary invocation (once at startup)

```python
merge_base = git("merge-base", base_ref, "HEAD")   # explicit; SHA needed for header/cache/scope anyway

subprocess.run([
    "git", "-C", repo_root,
    "-c", "core.quotePath=false",
    "diff", "--no-ext-diff", "--no-color",
    "--unified=0", "--find-renames",
    merge_base, "HEAD",
    "--",
], capture_output=True, text=True, check=True)
```

`-U0` output is proportional to *changed* lines, not file sizes — a 20k-line diff is
~1–2 MB of text and parses in well under a second. This is what keeps startup O(diff)
rather than O(every touched file), which matters at the 10–20k-line scale this tool
targets.

Three flags are load-bearing and cause silent breakage if omitted:

- **`--no-ext-diff`** — a `diff.external` in user config, or a `.gitattributes` diff
  driver, otherwise hijacks the output format and the parser receives garbage.
- **`core.quotePath=false`** — non-ASCII paths otherwise arrive octal-escaped
  (`"caf\303\251.txt"`) and match nothing.
- **`--find-renames`** — emits `rename from`/`rename to`, so a moved file keeps its
  review state instead of appearing as a delete plus an add.

### One invocation, one source of block identity

| Call | When | Used for |
|---|---|---|
| `-U0` over whole diff | Once, at startup | **Block identity** (hashes), per-file counts, left nav, progress |
| `git show <head_sha>:<path>` | On file open, cached | The unchanged lines between the runs |

**Never a second `git diff` for display.** The first cut asked git for the same diff
again at `--unified=999999` and matched rows to blocks by line number. That is unsound:
git's edit script is not stable across context widths. On a real change, a `)` line
identical on both sides came back as *context* under `-U0` and as a delete/add pair at
full context — 52 changed lines against 54. The two extra rows fell inside no block and
`verify_tagging` raised through the HTTP layer as a 500 that took out the file view.
Display rows are now replayed from the `-U0` runs themselves: everything between two runs
is unchanged by definition and can be read off the new side, and deleted text lives in
the runs. Rows and blocks come from one edit script, so they cannot disagree.

An earlier draft used `-U3` for the identity pass and mapped git's hunk grouping onto the
full-context display — self-described as the riskiest code in the project, because `-U3`
hunk extents include context lines and merge under git's own heuristics, neither of which
the display pass reproduces. That mapping is gone. Instead:

**Blocks are defined by us, not by git.** A `-U0` hunk is exactly one run of changed
lines with exact new-side positions. A **block** is one or more such runs, merged when
separated by ≤ 3 unchanged lines (so a one-line gap doesn't produce two checkboxes). The
merge rule is ours and deterministic, so identity is stable across runs and rebases
without depending on git's hunk-splitting behaviour.

**Mapping is arithmetic, not inference.** Each block carries its new-side line range
directly from the `-U0` headers; tagging rendered rows is a line-number lookup. A
deletion-only block (`target_length = 0`) anchors to the preceding new-side line — the
one place to be careful; assert it against pure-deletion and pure-addition fixtures.

A split block's sub-runs anchor their zero-length side loosely (`tgt_start + adds`), which
is fine for tagging — an empty range tags nothing — but wrong for replay. `FileDiff.runs`
therefore keeps git's runs as they arrived, before `split_blocks` cut any of them.

**Renames need no per-file pathspec** now that display replays the startup pass: the
`-U0` call already ran `--find-renames` over the whole diff, so a renamed file's runs are
in hand and only its new-side content is read.

**Pure renames** with no content change emit no hunks under either pass — synthesize the
`hunk_id = '*'` item (§14) and fall back to `git show <head_sha>:<path>` for content.

### Immutability

Committed-only means that for a given `(merge_base_sha, head_sha)` the diff cannot change.
Cache `/api/file` responses indefinitely keyed on those SHAs — no invalidation, no
staleness checks. Amending and rebasing mints new `head_sha`s, so the cache directory
grows monotonically: prune cache dirs untouched for 30 days in the same startup pass that
prunes the DB (§7).

### Uncommitted work

Invisible by design, which reliably produces "but I already fixed that." At startup run
`git status --porcelain --untracked-files=no`; if non-empty, show a header banner —
*3 files have uncommitted changes, not shown* — hoverable for the list.

### Staleness

Always display the resolved merge-base SHA and its committer date in the header, so a
months-old `origin/master` is visible rather than invisible.

---

## 5. Scope key

```
{repo_root}|{base_ref}|{branch_name}
e.g.  /home/you/proj|origin/master|feature/pipeline-v2
```

**Symbolic, not resolved SHAs.** This inverts the usual advice and is deliberate.

A stack's `HEAD` moves on every amend and every rebase onto a moved master. If the scope
key contained the HEAD SHA, every rebase would present a brand-new comparison with zero
review state — the feature would evaporate exactly when it's most valuable.

Instead the key is stable and **all invalidation is delegated to the content hashes** in
§7. Rebase the stack, reopen, and every block whose content didn't change is still marked.
Only genuinely changed blocks return as unreviewed. That is `git range-diff` semantics for
free, without implementing range-diff.

Two consequences, both acceptable:

- Detached HEAD has no branch name — fall back to the merge-base SHA and say so in the UI.
- A block that is reviewed, reverted, then reintroduced identically comes back already
  reviewed. Correct under the content rule; mildly surprising in practice.

---

## 6. Review states

Per `(scope, path, hunk_id)`:

| State | Set by | Meaning |
|---|---|---|
| **unseen** | default | Never scrolled into view |
| **seen** | automatic | Eyes passed over it |
| **reviewed** | explicit | Signed off |

Not a strict ladder: un-reviewing returns a block to **seen**, not unseen, since you
demonstrably saw it.

### Auto-seen

Uniform line-height makes visibility exactly computable from `scrollTop` — track
seen-ness with one rAF-throttled scroll handler doing line-range arithmetic, **not**
`IntersectionObserver`. IO observes DOM nodes, and under virtualization (mandatory at
this diff scale, §9) most rows have no node; the taller-than-viewport case needed scroll
math anyway. One mechanism, works identically virtualized or not. A block becomes seen
when either:

- it is fully inside the computed visible line range for a **500 ms dwell**, or
- its top edge passes above the viewport top (covers blocks taller than the viewport,
  which can never satisfy the first condition)

Suppressed while the window lacks focus.

**Known weakness:** fast-scrolling a long file marks everything seen, degrading signal to
noise. The dwell and focus checks handle most of it. Keep `--no-auto-seen` as an escape
hatch, and ensure nothing important depends on seen-ness — it is advisory. `reviewed` is
the state that counts.

### Write batching

Explicit reviews are deliberate single clicks — POST immediately, optimistic UI update, no
spinner. Auto-seen fires constantly — queue block IDs, flush every 2 s and on
`visibilitychange` via `sendBeacon`. Two implementation notes: `sendBeacon` sends
`text/plain` unless the payload is a `Blob` with an explicit `application/json` type, so
do that (or accept text/plain server-side); and each flush is one SQLite transaction via
`executemany`, since a fast scroll through a 20k-line diff can queue hundreds of blocks.

---

## 7. Data model

```sql
CREATE TABLE block_state (
  scope       TEXT NOT NULL,
  path        TEXT NOT NULL,   -- target path, post-rename
  hunk_id     TEXT NOT NULL,   -- see below; '*' for whole-file items
  seen_at     TEXT,            -- ISO8601; NULL = never viewed
  reviewed_at TEXT,            -- ISO8601; NULL = not signed off
  PRIMARY KEY (scope, path, hunk_id)
);
CREATE INDEX bs_scope ON block_state(scope);
```

Setting `reviewed_at` backfills `seen_at` if null, so bulk file-level review never leaves
incoherent rows.

### Block identity

A block is the `-U0` runs merged per §4. Its hash covers the changed lines only, with
their `+`/`-` prefixes, in order:

```python
body = "".join(f"{line.line_type}{line.value}" for run in block for line in run)
hunk_id = hashlib.sha256(body.encode()).hexdigest()[:16]
```

**Line numbers are excluded** (there is no `@@` header in the body), so an edit elsewhere
in the file doesn't invalidate this block. This is what makes auto-invalidation free: an
edited block hashes differently, its ID is simply absent from the table, and it renders
unreviewed. No change detection, no invalidation pass.

**Tradeoff:** context lines are not in the hash (with `-U0` they aren't available), so a
block whose *surroundings* changed stays reviewed. That is the weaker of the two identity
choices, and mostly the more pleasant one: fewer spurious invalidations after rebases. If
it proves too weak, mixing in the merged-run gap structure or fetching ±3 context lines
via the display pass are both incremental changes.

**Identical blocks within one file** — repeated boilerplate edits do happen — would
collide on the primary key and share state. Disambiguate by occurrence: the second and
later identical blocks in file order get `-2`, `-3`, … suffixed to the ID. (Reordering
two identical blocks then swaps their state; acceptable.)

`path` is in the primary key even though hashes are near-unique: identical blocks do occur
across files (license headers, boilerplate imports) and marking one must not mark all.

Prune rows with `seen_at` and `reviewed_at` both older than 60 days on startup.

---

## 8. API

| Route | Behaviour |
|---|---|
| `GET /api/files` | `-U0` parse only: file list, block IDs, state, per-file counts. No line content. |
| `GET /api/file?path=` | The startup runs replayed against the file's new-side content: full lines, change blocks tagged with block IDs. Cached. |
| `POST /api/seen` | Batch `{blocks: [{path, hunk_id}]}`. Idempotent, `sendBeacon`-friendly. |
| `POST /api/reviewed` | Single or list. Backfills `seen_at`. |
| `DELETE /api/reviewed` | Demotes to seen. `?file=` and `?all=` variants. |

Bind `127.0.0.1` only, and reject requests whose `Host` header isn't
`localhost`/`127.0.0.1` — a two-line DNS-rebinding guard for a server that can read your
source tree. Add gzip middleware (`GZipMiddleware`, one line): a `/api/file` payload for
a 10k-line file with tokens is a few MB raw and compresses ~10×.

### `/api/files`

```json
{
  "scope": "/home/you/proj|origin/master|feature/pipeline-v2",
  "base_sha": "a1b2c3d", "base_date": "2026-07-28T11:04:00Z",
  "head_sha": "9f8e7d6",
  "dirty_files": ["src/loader.py"],
  "stats": {"blocks": 57, "seen": 34, "reviewed": 18},
  "files": [{
    "path": "src/pipeline.py",
    "old_path": null,
    "status": "modified",
    "added": 42, "removed": 7,
    "blocks": 6, "seen": 4, "reviewed": 2
  }]
}
```

### `/api/file`

```json
{
  "path": "src/pipeline.py",
  "lang": "python",
  "total_lines": 412,
  "highlighted": true,
  "blocks": [
    {"hunk_id": "3f9a2b1c8d4e5f60", "start": 40, "end": 47,
     "kind": "mixed", "seen": true, "reviewed": false}
  ],
  "lines": [
    {"old": 41, "new": 42, "type": "add",
     "t": [["k","def"],["w"," "],["nf","transform"],["p","("],["n","row"],["p","):"]]}
  ]
}
```

`type` is one of `context` / `add` / `del`. `t` carries highlight tokens; when
highlighting is unavailable the row uses `"text"` instead.

---

## 9. Frontend

Single `index.html`, no build step. Left nav plus diff pane plus minimap.

### Left nav

```
● src/pipeline.py          ▓▓▓▓▓░░░  +42 −7
○ src/loader.py            ▓▓░░░░░░  +8  −0
✓ tests/test_pipeline.py   ▓▓▓▓▓▓▓▓  +91 −3
```

Two-fill progress bar per file: solid = reviewed, ghost = seen-not-reviewed, empty =
unseen. Readable at a glance without a legend. The leading glyph collapses it to a single
symbol — hollow (untouched), half (in progress), check (fully reviewed).

- Click name → open file
- Click glyph → toggle whole file reviewed
- `h` → hide fully-reviewed files
- Filter box at top, focused by `g`
- Renames show `old → new`, truncated **from the left** (path tails distinguish)

Header: `18/57 reviewed · 34 seen`, base SHA and date, dirty-file banner if applicable.

### Left nav (as built)

The list is a collapsible folder tree, not a flat path list — horizontal space is
scarce. Single-child directory chains compress (`src/app/`), directory rows carry
aggregated two-fill progress bars, collapse state persists in `localStorage`, and
filtering switches to a flat full-path list. Files show basenames; the full path (and
`old → new` for renames) lives in the tooltip.

### Diff pane

Whole file, unified. Long lines soft-wrap (`word-break: break-all`, so breaks land on
column boundaries). Wrapping means row heights vary — but monospace makes them exactly
computable: `ceil(display_columns / columns_per_row)` wrapped lines per row, with glyph
width measured at runtime (and re-measured on font switch, once webfonts load). A
prefix-sum offset table replaces `row × line-height` as the single source of scroll
geometry; virtualization, the minimap, auto-seen and block scrolling all read it.

Virtualize line rendering **always**, not just past a threshold: at the 10–20k-line
diffs this tool targets, single files routinely exceed what the DOM renders smoothly, and
one always-on code path is simpler than two (no "small-file mode" to keep working). Fixed
line-height makes `render(visibleRange ± 100)` straightforward — a spacer div sized
`totalLines × lineHeight`, absolutely positioned rows. The minimap and seen-tracking are
computed from line numbers, not the DOM, so neither notices.

### Minimap

Fixed 14 px strip on the right, full height of the diff pane. Uniform line-height makes
the geometry exact:

```js
mark.top    = (block.startLine / file.totalLines) * trackHeight;
mark.height = Math.max(3, (block.lineCount / file.totalLines) * trackHeight);
```

Encoding — hue carries change kind, fill carries review state, ordered so that
*remaining work* draws the eye:

| | |
|---|---|
| Hue | green = additions, red = deletions, amber = mixed |
| unseen | solid, full saturation |
| seen, unreviewed | solid at 45 % |
| reviewed | 2 px hollow outline only |

The minimap therefore doubles as the progress indicator, which is the real payoff.

- Translucent overlay box shows the current viewport
- Click on the track → `scrollTop = (clickY / trackHeight) * scrollHeight`
- Click directly on a mark → scroll that block to **1/3 viewport height**, not centre —
  it reads better

Keep the native scrollbar in v1 and sit the minimap beside it. Replacing it means
implementing thumb drag, wheel passthrough and touch — a day of work for polish you may
not miss. Straightforward to upgrade later.

### Theming

Four themes: **light**, **dark**, **solarized** (dark variant; the light variant is a
five-line variable block away if wanted), **space** (near-black, blue-tinted dark).

One mechanism carries all of it — every colour in the app is a CSS custom property, and a
theme is a variable block under an attribute selector:

```css
:root[data-theme="solarized"] {
  --bg: #002b36; --fg: #839496; --gutter: #073642;
  --add-bg: rgba(133,153,0,.08); --del-bg: rgba(220,50,47,.08);
  --add-sign: #859900; --del-sign: #dc322f; --mixed: #b58900;
  /* … nav, minimap, toast, viewport-box */
}
```

Rules that keep four themes from becoming four bug surfaces:

- **No colour literals outside theme blocks.** Components reference variables only; a new
  theme is one block, zero component edits.
- The §11 constraint — add/del row backgrounds at 6–8 % opacity so syntax colours
  survive — is **per-theme tuned**, not one shared rgba: solarized and space have dark,
  tinted bases where the same alpha reads differently. The minimap hues (green/red/amber)
  are also theme variables, mapped to each palette's own accents (solarized uses its
  `green`/`red`/`yellow`, not generic web colours).
- **Syntax CSS is generated, not hand-written.** The server emits one stylesheet with each
  Pygments style scoped under its theme:
  `HtmlFormatter(style=s).get_style_defs(f'[data-theme="{t}"]')`, concatenated and served
  as `GET /theme.css`, cached forever. Pairings: light → `default`, dark → `github-dark`,
  solarized → `solarized-dark`, space → `nord` — all ship with Pygments; verify the set
  against the pinned version in milestone 5c and swap by name if one looks wrong.
- Default theme from `prefers-color-scheme` (light → light, dark → dark); explicit choice
  persisted in `localStorage` and set as `data-theme` on `<html>` **before first paint**
  (inline script in `<head>`) to avoid a flash of wrong theme.

### Typography

Two bundled monospace faces, **JetBrains Mono** (default) and **Ubuntu Sans Mono**, both
OFL-licensed: vendor the woff2 files under `static/fonts/` — no CDN, keeping the
no-network, no-build rule. Font choice is a header toggle persisted in `localStorage`,
implemented as `--mono-font` on `:root`.

The one load-bearing rule: **line-height is a fixed pixel value, not `normal`**, and all
scroll geometry derives from the offset table (§ diff pane), whose inputs are that pixel
value plus the measured glyph width. The two fonts have different natural metrics, so a
font switch re-measures and rebuilds the offsets once `document.fonts.ready` resolves —
an em-derived line-height or a stale glyph width would silently skew every `scrollTop`
computation. `--line-height: 20px` is read via `getComputedStyle` rather than duplicated
in JS.

---

## 10. Line references

Format is `path:line` — the grep/gcc/ripgrep convention, understood by every LLM and by
every editor's jump-to-line. Paths are **relative to repo root**; absolute paths leak your
home directory and are less useful pasted.

```
src/pipeline.py:42
src/pipeline.py:42-58
```

| Gesture | Result |
|---|---|
| Click line number | copy `path:42` |
| Shift-click | extend range, copy `path:42-58` |
| Drag across gutter | same, live range |
| `y` | copy ref **plus line content** as a fenced block |
| `Y` | copy refs for every unreviewed block in the file |

`y` is the primary LLM affordance — a bare ref makes the model guess, ref-plus-content
doesn't. The fence language comes from the resolved lexer's canonical alias, so it is
consistent with what the file view showed:

````
src/pipeline.py:42-45
```python
    for row in batch:
        yield transform(row)
```
````

**Clipboard:** `navigator.clipboard.writeText()` requires a secure context.
`http://localhost` and `http://127.0.0.1` both qualify, so this works — but it fails
silently if ever bound to a LAN IP. Fall back to a toast containing the ref in a
pre-selected `<input>` so Ctrl-C works. Always show a brief toast confirming what was
copied; silent clipboard writes feel broken.

---

## 11. Syntax highlighting

Server-side Pygments, consuming the **token stream** rather than its HTML formatter.

### Why not client-side

highlight.js and Prism emit HTML with spans that **cross line boundaries** — a multi-line
Python docstring or JSX block becomes one `<span>` wrapping ten lines. Splitting by `\n`
breaks the markup, and every workaround (re-opening unclosed spans per line) is precisely
the fiddly failure mode to avoid. Shiki solves it properly but needs WASM and a bundler,
breaking the no-build-step rule.

Pygments sidesteps it because tokens can be split at newlines *before* emission, making
per-line output correct by construction:

```python
from pygments.lexers import get_lexer_for_filename

def tokenize_lines(text: str, filename: str) -> list[list[tuple[str, str]]]:
    """→ per line, a list of (css_class, text) pairs."""
    lexer = get_lexer_for_filename(filename, stripnl=False)
    lines, cur = [], []
    for tok_type, value in lexer.get_tokens(text):
        cls = _short_class(tok_type)          # Token.Keyword → "k"
        for i, piece in enumerate(value.split("\n")):
            if i:
                lines.append(cur); cur = []
            if piece:
                cur.append((cls, piece))
    if cur:
        lines.append(cur)
    return lines
```

Use Pygments' own class names verbatim — then any Pygments style works unmodified, and
the per-theme scoped stylesheets of §9 (`get_style_defs('[data-theme="…"]')`) come for
free.

### Two-pass alignment

The new-side file doesn't contain deleted lines, so one pass can't cover both sides.
Reconstruct both from the display rows — new side is context+adds, old side is
context+dels — lex each independently, then map onto display rows: context and `+` rows
take new-side tokens, `−` rows take old-side. Two lexer passes per file, ~20 extra lines,
and correct even when a deleted line sat inside a multi-line string that no longer exists.

(The one-pass alternative — lex new side only, render deleted lines plain — halves the work
and is defensible since deleted code is being discarded, but looks unfinished next to
highlighted additions.)

### Languages

| Extension | Lexer | Note |
|---|---|---|
| `.py` | `PythonLexer` | — |
| `.html` | `HtmlLexer` | Delegates `<script>` / `<style>` bodies automatically |
| `.js`, `.mjs` | `JavascriptLexer` | Template literals handled |
| `.ts` | `TypeScriptLexer` | — |
| `.jsx`, `.tsx` | **verify at build time** | See below |

**JSX needs checking.** Recent Pygments ships a dedicated JSX lexer, but the version that
introduced it and its import path should be confirmed empirically — call
`get_lexer_for_filename("x.jsx")` and inspect what comes back before relying on it. If
absent or poor, `JavascriptLexer` handles JSX acceptably: expressions, keywords and strings
are correct; JSX tags lex as operators and identifiers rather than markup. Passable, not
pretty. **Pin the Pygments version** in `pyproject.toml` once behaviour is confirmed.

Unknown extensions raise `ClassNotFound` → fall back to plain rows. Wrap every lexer call
in try/except; a lexer failure must never take down a file view.

### Highlighting vs diff backgrounds

These compete for the same visual channel, and this is where naive implementations look
bad. Line backgrounds must be weak enough that syntax colours survive on top:

- add/delete backgrounds at **6–8 % opacity**, not the 20 %+ of GitHub-style flat diffs
- diff signal carried mainly by the **gutter sign column** (`+`/`−` in saturated colour)
  and the minimap, not by row fill
- syntax palette at full saturation — it does the fine-grained work

If intra-line word diff is ever added it needs a third channel: a stronger background patch
on the changed span only. Highlighting *or* word diff, or word diff with syntax suppressed
inside the patch. Do not stack all three.

### Caching and guards

Immutable diff → cache permanently:

```
~/.cache/seendiff/{base_sha[:12]}-{head_sha[:12]}/{path_hash}.json
```

Pygments is fast but not free, and pathological input exists:

- skip highlighting above **10k lines or 1 MB**; render plain, note it in the header
- **minified JS** — one 40k-character line will pin a CPU. Detect mean line length > 500
  and skip.
- lex on a thread pool if a large file blocks the event loop — `run_in_threadpool` in
  FastAPI is a one-liner

Keep `--no-highlight` wired from the start. It's the fastest way to tell whether a
rendering oddity is the diff logic or the lexer.

---

## 12. Keymap

Arrow keys are the primary navigation: horizontal moves within the file, vertical moves
through the file stack. This frees them from scrolling — the wheel, `j`/`k` and
`d`/`u` cover that.

| Key | Action |
|---|---|
| `←` / `→` | prev / next change block **in the same file** (stops at file edges) |
| `↑` / `↓` | prev / next file in the stack |
| `R` | mark current block reviewed, advance to next unreviewed (toggles off if already reviewed) |
| `n` / `N` | next / prev **unreviewed** block, crossing into the next file at end of file |
| `j` / `k` | line scroll |
| `d` / `u` | half-page scroll |
| `V` | mark every seen-but-unreviewed block in this file reviewed |
| `f` | mark whole file reviewed, jump to next file |
| `y` | copy ref + content for current selection |
| `Y` | copy refs for all unreviewed blocks in file |
| `g` | focus sidebar filter (fuzzy file jump) |
| `h` | toggle hide fully-reviewed files |
| `t` | cycle theme (light → dark → solarized → space) |
| `F5` / `⌘R` | reload diff (browser reload is fine — state is server-side; preserve scroll by block ID via the URL fragment) |

Two bindings carry most of the ergonomic value:

- **`n` crossing file boundaries** makes a 20-file stack reviewable without touching the
  sidebar: `n`, read, `R`, `n`, read, `R`. Arrows are the deliberate, bounded version of
  the same moves.
- **`V`** signs off a file wholesale after reading it top to bottom, but only promotes
  blocks actually scrolled past — meaningfully different from `f`.

Arrow handling must `preventDefault()` only when the diff pane has focus and the filter
box doesn't — otherwise typing in the filter breaks. Lowercase `r` is deliberately
unbound: adjacent to `R`, and a mis-hit that reloaded mid-review would be irritating.

---

## 13. Build order

| # | Milestone | ~Lines |
|---|---|---|
| 1 | `git.py`: resolve merge-base, run `-U0` diff, parse, merge runs into blocks, print block IDs to stdout | 120 |
| 2 | `store.py` + scope key derivation, exercised from a REPL | 60 |
| 3 | `git.py`: `-U999999` per file (both paths for renames), tag rows with block IDs by line range | 70 |
| 4 | `server.py`: `/api/files`, `/api/file` | 90 |
| 5 | Frontend: left nav + whole-file render, plain rows, no interactivity | 160 |
| 5b | `highlight.py`: two-pass tokenizer, extension map, guards | 90 |
| 5c | Wire format `t` rows, client span assembly, `/theme.css` (4 scoped Pygments styles), theme + font toggles | 100 |
| 6 | Minimap: marks, viewport box, click-to-jump | 110 |
| 7 | Explicit reviewed: click, POST, nav bars, minimap states | 110 |
| 8 | Auto-seen: IntersectionObserver, dwell, batched flush | 90 |
| 9 | Line refs: selection, clipboard, toasts, fenced copy | 110 |
| 10 | Keymap + change navigation + `V` | 130 |
| 11 | `cli.py`, packaging, dirty banner, virtualization | 90 |

Roughly 1300 lines total.

**Checkpoints worth pausing at:**

- **After 1** — assert ID stability, the property everything rests on: in a scratch repo,
  amend an unrelated file, insert lines above a block, rebase onto a moved base — block
  IDs for untouched changes must not move. Also fixture-test deletion-only and
  pure-addition blocks, the two anchoring edge cases.
- **After 3** — assert row tagging: every `+`/`−` row in the full-context output is
  covered by exactly one block from the `-U0` pass, and no block is empty. Cheap
  invariant, catches any drift between the two passes. Run it against this repo's own
  history, not just fixtures.
- **After 5** — a working whole-file diff viewer. You'll know whether the layout and block
  granularity feel right before building the state layer on top.
- **7 before 8, deliberately** — get the state layer correct under explicit clicks, where
  every write is intentional and verifiable, before adding a source of automatic writes.

---

## 14. Edge cases

Handle in v1:

- **Binary, mode-only and submodule changes** produce zero hunks, so they vanish from the
  file list unless you synthesize one item with `hunk_id = '*'`. Label them explicitly —
  unlabelled submodule pointer diffs look like corruption.
- **`origin/master` unresolvable** — no remote, or renamed default branch. Try
  `origin/master`, `origin/main`, then local `master`, `main` (covers remote-less repos);
  fail with a message naming everything tried, not a traceback.
- **Empty diff** — an explicit "nothing to review" state, not a blank page.
- **Renames** — `unidiff` exposes `source_file` / `target_file`. Key state on target path,
  display `old → new`.
- **Detached HEAD** — no branch name for the scope key; fall back to merge-base SHA and
  say so.
- **Pure renames** — no hunks at all; fall back to `git show <sha>:<path>` for content.

Deferred, with reasons:

- **Conflicted files** — combined diff format (`@@@`, two-char prefixes) is not parseable
  by `unidiff`. Irrelevant here since we're not merging; if `git status --porcelain` shows
  `U` states, warn and skip.
- **Uncommitted changes** — out of scope by decision; surfaced as a banner.
- **Word-level intra-line diff** — conflicts with syntax highlighting for visual channel
  (§11).
- **Side-by-side view** — no horizontal budget alongside minimap and refs.
- **Ref basket** — `a` to accumulate refs across files, `A` to copy the set. ~40 lines,
  genuinely useful for assembling LLM context from several places. Skipped in v1.
- **Comments, per-commit attribution** (`git log -L`), multi-repo dashboard.
