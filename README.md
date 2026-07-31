# seendiff

A local, point-in-time diff reviewer for a stack of commits.

Renders your branch's full diff against `origin/master`, shows each changed file **whole**
(not as hunk fragments with three lines of context), remembers which changes you've
looked at and which you've explicitly signed off, and makes it easy to copy precise line
references for pasting into an LLM.

It's the viewed-checkbox workflow of a forge PR view, without pushing a branch or opening
a PR — and unlike Meld, `git difftool`, or `git range-diff`, the review state survives
closing the window.

Not a merge tool: no editing, no conflict resolution, no comments. It's a reader.

---

## Install

Requires Python ≥ 3.10 and `git`.

```bash
git clone git@github.com:currents-abhishek/seendiff.git
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
uv tool install git+https://github.com/currents-abhishek/seendiff
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
| `g` | focus the file filter |
| `h` | hide fully-reviewed files |
| `t` | cycle theme &nbsp;•&nbsp; `?` help &nbsp;•&nbsp; `X` reset all state |

**Seen** happens on its own — a block scrolls through the viewport, it's seen.
**Reviewed** is always deliberate.

## Architecture

Roughly 2,000 lines total, six Python modules and one HTML file. No build step, no
frontend framework, no bundler, no node_modules.

```
git ──► git.py ──► server.py ──► static/index.html
                       │            (fetches JSON, renders)
                       ▼
                    store.py ──► ~/.local/state/seendiff/state.db
```

| file | lines | what it does |
|---|---|---|
| `git.py` | 416 | Runs `git diff` via subprocess, parses it with `unidiff`, groups hunks into **blocks**, expands submodule pointers, and reconstructs each file's *whole* post-image with every line tagged added / removed / context. |
| `server.py` | 313 | FastAPI app: a handful of JSON endpoints plus the static page. Holds the diff in memory and recomputes only when HEAD moves. |
| `store.py` | 108 | SQLite. One table, no ORM, no migrations. |
| `highlight.py` | 109 | Pygments tokenization per line, with bail-outs for minified or enormous files. |
| `cli.py` | 72 | argparse → pick a free port → open a browser → `uvicorn.run`. |
| `static/index.html` | 1069 | The entire UI: markup, CSS, and vanilla JS in one file. |

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

State lives in `~/.local/state/seendiff/state.db` and is pruned after 60 days;
the rendered-file cache in `~/.cache/seendiff/` after 30.

## Design notes

[`PLAN.md`](PLAN.md) is the original build brief — motivation, the alternatives that were
rejected, and the reasoning behind each locked decision. Deviations made during
implementation are noted inline.

## Fonts

Bundles [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) (Apache-2.0) and
[Ubuntu Sans Mono](https://github.com/canonical/Ubuntu-Sans-fonts) (UFL-1.0).
