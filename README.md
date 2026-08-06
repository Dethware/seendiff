# seendiff

A local, point-in-time diff reviewer for a stack of commits.

![MIT](https://img.shields.io/badge/license-MIT-blue) ![Python](https://img.shields.io/badge/python-%E2%89%A53.10-blue) ![No build step](https://img.shields.io/badge/build%20step-none-green)

Renders your branch's full diff against `origin/master`, shows each changed file **whole**
(not as hunk fragments with three lines of context), remembers which changes you've
looked at and which you've explicitly signed off, and makes it easy to copy precise line
references for pasting into an LLM.

It's the viewed-checkbox workflow of a forge PR view, without pushing a branch or opening
a PR — and unlike Meld, `git difftool`, or `git range-diff`, the review state survives
closing the window.

Not a merge tool: no editing, no conflict resolution, no comments. It's a reader.

<img width="1848" height="918" alt="image" src="https://github.com/user-attachments/assets/feba354a-0070-4e7f-8ec8-1e4c04e126d1" />


---

## Install

Requires Python ≥ 3.10 and `git`.

```bash
git clone git@github.com:acme-dot-bot/seendiff.git
cd seendiff
python3 -m venv .venv
.venv/bin/pip install -e .
```

Then, from *any* git repo:

```bash
/path/to/seendiff/.venv/bin/seendiff
```

Nicer: put it on your `PATH` once and forget the venv exists.

```bash
ln -s /path/to/seendiff/.venv/bin/seendiff ~/.local/bin/seendiff
```

Or skip the clone entirely with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/acme-dot-bot/seendiff
```

There is nothing to configure. No config file, no daemon, no server to keep running.

## Use

```bash
cd ~/some/repo
seendiff                 # HEAD vs origin/master (with fallbacks)
seendiff main            # ...vs an explicit base ref
seendiff --fetch         # git fetch first (never implicit)
seendiff --clear         # drop review state for this branch, then start
```

It picks a free port, starts a localhost server, and opens your browser. Ctrl-C to stop.

Useful flags: `--port`, `--no-browser`, `--no-highlight`, `--no-auto-seen`.

### Search

`Ctrl-F` opens the search bar. It searches **every changed file**. Results land
in a right-hand panel — click one to jump, `⏎` / `⇧⏎` walk them, `Esc` closes.
Matches in the open file are marked in place as you type.

The browser's own find can't help here: the code pane is virtualized, so only
the rows on screen exist in the DOM. seendiff matches against the diff data
instead, marks every hit in place (keeping its syntax colour), and ticks them
on the minimap. Whole-change search runs server-side via `/api/search`, since
the page only ever holds one file.

Queries are literal and smart-cased — all-lowercase matches any case, any
capital makes it case-sensitive. `.*` toggles regex. Removed lines are
searchable too. Reopening with `Ctrl-F` keeps the last query, its results and
the panel; a new query resets them (and brings a dismissed panel back).

### Themes

`t` cycles six themes — **light**, **dark**, **solarized-light**, **solarized-dark**,
**space** and **hailmary**. Each carries a matching Pygments palette, so syntax colours
change with the chrome rather than staying bolted to one scheme.

The sidebar progress bar is a neon meter: an analog tick row on space and hailmary, a
solid bar elsewhere, and its glow stays off until you're half done, then ramps up as the
review finishes. The light themes run flat, with no glow at all.

Theme, font, layout, sidebar width and collapsed folders persist **server-side**, not in
`localStorage` — every run gets a fresh port, and browser storage is scoped per origin,
so it alone would forget.

### Walkthroughs

seendiff can play back a *scripted code walkthrough* — an ordered, layered
explanation written as JSON (usually by an agent; see
[`CODEWALKTHROUGH.md`](CODEWALKTHROUGH.md) and the
[worked example](examples/seendiff-review-state.json)):

```bash
seendiff --walkthrough .git/seendiff-walkthrough.json   # play it back
seendiff --check-walkthrough path/to/wt.json            # validate, exit 0/1
```

A bar under the code pane carries the current step's prose; `←`/`→` step
through the script, `[`/`]` jump acts, `.` toggles detail steps, `w` hides the
bar, and **dismiss** drops the walkthrough entirely and returns to the plain
diff. Works with or without a diff — any file in the repo is openable while a
walkthrough is loaded.

The walkthrough file may be **empty** (`{}`, no steps, or not written yet).
That's not an error: seendiff opens as a plain diff reviewer and watches the
file, so an agent can write a script into it later and every open tab picks it
up within a couple of seconds — no relaunch, no new tab. That's how the skill
runs: one server per conversation, launched with an empty walkthrough up front.

To let Claude show diffs and write walkthroughs for any repo, symlink the skill
once:

```bash
ln -s "$(pwd)/skills/seendiff" ~/.claude/skills/seendiff
```

### Keys

| | |
|---|---|
| `←` `→` | previous / next change block |
| `↑` `↓` | previous / next file |
| `n` `N` | next / previous **unreviewed** block |
| `R` | toggle reviewed on the focused block, jump to next unreviewed |
| `V` | mark every *seen* block in this file reviewed |
| `f` | mark the whole file reviewed, move to the next file |
| `y` | copy a `path:line` ref for the focused block, with its content |
| `Y` | copy refs for everything still unreviewed |
| `j` `k` `d` `u` | scroll |
| `Ctrl-F` | search all changed files — `⏎`/`⇧⏎` next / previous |
| `g` | focus the file filter |
| `h` | hide fully-reviewed files |
| `t` | cycle theme &nbsp;•&nbsp; `?` help &nbsp;•&nbsp; `X` reset all state |

**Seen** happens on its own — a block scrolls through the viewport, it's seen.
**Reviewed** is always deliberate.

## Architecture

**A Python HTTP server, one hand-written HTML file, and SQLite.** That's the whole stack:

- **Server** — FastAPI on uvicorn, bound to `127.0.0.1`. Serves a handful of JSON
  endpoints and one page.
- **Frontend** — vanilla JavaScript and CSS in a single `index.html`. No framework, no
  bundler, no build step, no `node_modules`.
- **State** — SQLite. One file, two tables, no ORM and no migrations.

**~4,000 lines of shipped code**, plus ~460 of tests. Four runtime dependencies:
FastAPI, uvicorn, `unidiff`, Pygments.

```
git ──► git.py ──────► server.py ──► static/index.html
                        │   ▲          (fetches JSON, renders)
        walkthrough.py ─┘   │
                            ▼
                        store.py ──► ~/.local/state/seendiff/state.db
```

| file | lines | what it does |
|---|---|---|
| `static/index.html` | 2326 | The entire UI: markup, CSS and vanilla JS in one file. Virtual scroller, side-by-side and unified views, canvas minimap, search, six themes, walkthrough player. |
| `server.py` | 526 | FastAPI app. Holds the diff in memory, recomputes only when HEAD moves, disk-caches rendered files, and inlines the syntax palette into the page. |
| `git.py` | 446 | Runs `git diff` via subprocess, parses it with `unidiff`, groups hunks into **blocks**, expands submodule pointers, and reconstructs each file's *whole* post-image with every line tagged added / removed / context. |
| `walkthrough.py` | 348 | Loads and validates walkthrough scripts; every line reference is checked against the real diff before anything renders. |
| `store.py` | 126 | SQLite: review state and UI preferences. |
| `cli.py` | 117 | argparse → pick a free port → open a browser → `uvicorn.run`. |
| `highlight.py` | 109 | Pygments tokenization per line, with bail-outs for minified or enormous files. |

A few decisions worth knowing:

**Blocks, not hunks.** A hunk is a diff-format artifact — its boundaries move when you
change the context setting. A block is a maximal run of adjacent changed lines. Each gets
a stable `hunk_id` derived from its content, so review state survives a rebase that
shifts line numbers around.

**Scope is symbolic.** State is keyed on `repo | base_ref | branch`, *not* on commit
SHAs. Amend, rebase, or reword and your review state is still there.

**Diff is immutable per (merge-base, HEAD).** So it's computed once and cached in memory;
requests only re-stat the working tree for the dirty-files banner.

**The frontend is a virtual scroller.** Files run to tens of thousands of lines, so only
the visible rows are in the DOM. Row geometry is measured from real glyph metrics — the
layout is remeasured once webfonts land — which is also what makes the canvas minimap
line up.

**Server binds 127.0.0.1 and rejects any `Host` header that isn't localhost**, so a
malicious page in another tab can't reach into your source tree via DNS rebinding.

**Nothing leaves the machine.** No telemetry, no network calls, no accounts. `git fetch`
runs only when you pass `--fetch`.

State lives in `~/.local/state/seendiff/state.db` and is pruned after 60 days;
the rendered-file cache in `~/.cache/seendiff/` after 30.

## Design notes

[`PLAN.md`](PLAN.md) is the original build brief — motivation, the alternatives that were
rejected, and the reasoning behind each locked decision. Deviations made during
implementation are noted inline.

## Development

```bash
.venv/bin/pip install -e . pytest
.venv/bin/python -m pytest tests/ -q
```

35 tests: the walkthrough validator, the search endpoint, preference storage, and the
search-match renderer — that last one runs under `node` (skipped if you don't have it)
and pulls the functions straight out of `index.html`, so it can't drift from what ships.

## License

[MIT](LICENSE).

Bundles two fonts under their own terms:
[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) (Apache-2.0) and
[Ubuntu Sans Mono](https://github.com/canonical/Ubuntu-Sans-fonts) (UFL-1.0).

## Options

<img width="1848" height="918" alt="image" src="https://github.com/user-attachments/assets/41525eb7-6938-4e71-8a45-17cff191b6b0" />
