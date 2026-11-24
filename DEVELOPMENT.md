# Developing seendiff

How seendiff is built and why. For using it, see [README.md](README.md); for how
it got this way (decisions, findings, rejected alternatives, with dates), see
[LOG.md](LOG.md). A reason that belongs to one piece of code is a comment next to
that code; this file covers what spans several.

## Layout

A Python HTTP server, one hand-written HTML file, and SQLite.

```
git ──► git.py ──────► server.py ──► static/index.html
                        │   ▲          (fetches JSON, renders)
        walkthrough.py ─┘   │
                            ▼
                        store.py ──► ~/.local/state/seendiff/state.db
```

| path | what it is |
|---|---|
| `seendiff/cli.py` | Entry point: arguments, base resolution, free port, browser, uvicorn, and the supervisor thread that exits with the last tab. |
| `seendiff/server.py` | FastAPI app: snapshots per diff mode, payload caches, every `/api/*` route, `Presence`. |
| `seendiff/git.py` | Everything that talks to git: diff sides and modes, the `-U0` pass, blocks and sections, display rows, commit and push. |
| `seendiff/store.py` | SQLite: review state and UI preferences. Two tables, no ORM, no migrations. |
| `seendiff/highlight.py` | Pygments token stream per display row, with guards. |
| `seendiff/walkthrough.py` | Loads and validates walkthrough scripts. Pure: no FastAPI. |
| `seendiff/gitcm.py` | `git cm`, installed as the `git-cm` console script. |
| `seendiff/static/index.html` | The whole UI: virtual scroller, unified and side-by-side views, minimap, search, themes, image view, commit panel, walkthrough player. Vanilla JS, no build step. |
| `seendiff/static/fonts/` | Bundled, subsetted webfonts; [their README](seendiff/static/fonts/README.md) covers sources and licences. |
| `skills/seendiff/SKILL.md` | The agent skill: how an agent opens seendiff and writes walkthroughs. Also the reference for the walkthrough file format. |
| `contrib/claude-commit-rule.md` | A `CLAUDE.md` rule that makes agents propose commit messages as `COMMIT_MSG_*.txt` files. |
| `examples/` | A worked walkthrough of seendiff's own review-state design; also a validator fixture. |
| `tests/` | pytest suite, plus one JS test run through node. |
| `packaging/arch/` | The personal Arch package, `seendiff-local`. |
| `docs/` | The seendiff.com landing page (GitHub Pages) and README images. Not documentation. |

## Ground rules

- **A reader, not an editor.** No editing, merging or conflict resolution.
  Conflicted paths are left out of every diff and only listed. The one write
  path is committing and pushing what the user staged.
- **No build step.** The frontend is one HTML file served as-is. Runtime
  dependencies are FastAPI, uvicorn, `unidiff` and Pygments.
- **Local only.** The server binds `127.0.0.1` and the `host_guard` middleware
  rejects any `Host` that isn't `localhost`, `127.0.0.1` or `[::1]`, so a page on
  another site can't reach the source tree through DNS rebinding.
  `SEENDIFF_ALLOW_HOST` adds one more allowed host, for tests and development
  only. Endpoints that change the repo also need the `X-Seendiff` header (see
  [Committing and pushing](#committing-and-pushing)). There is no telemetry, and
  `git fetch` runs only with `--fetch`.
- **Git is a subprocess we call.** Every call goes through `git._git`: UTF-8 with
  replacement (one stray byte must not fail a request), `core.quotePath=false`
  (non-ASCII paths arrive unescaped), and `GIT_OPTIONAL_LOCKS=0` (the UI polls
  git while the user runs their own git commands, so it never takes
  `index.lock`). Diffs always pass `--no-ext-diff` and `--find-renames`.

## Diff modes

A diff compares two **sides**. A side is a commit-ish or one of two pseudo-sides,
`git.INDEX` and `git.WORKTREE`, which every reader branches on before anything
reaches git as a revision. `git.mode_sides` maps the four modes:

| mode | old side | new side |
|---|---|---|
| `all` (default) | merge base | working tree |
| `committed` | merge base | HEAD |
| `staged` | HEAD | index |
| `unstaged` | index | working tree |

The **base** is the ref given on the command line, or the first of
`origin/master`, `origin/main`, `master`, `main` that resolves. A named base that
doesn't resolve is fatal; with no name given, seendiff falls back to `HEAD` so a
repo without a trunk still shows its uncommitted work. The diff is against
`git merge-base <base> HEAD` (three-dot semantics). Before the first commit, HEAD
diffs as the empty tree.

**Untracked files** appear in `all` and `unstaged` (unless `--no-untracked`).
`git diff` only knows indexed paths, so `git._untracked_index` copies the index to
a temporary file, marks untracked files intent-to-add there, and points
`GIT_INDEX_FILE` at it. The user's index is never touched, and the patches are
exactly what git emits once the files are added.

**Left out** of every diff: unmerged paths (a combined `--cc` patch, which neither
`unidiff` nor the viewer models) and `COMMIT_MSG_*.txt` proposals in the repo
root.

## One diff pass: runs, blocks, sections

Everything comes from one `git diff --unified=0 --full-index` per mode
(`git.raw_diff`), parsed with `unidiff` (`git.parse_diff`):

- A **run** is one `-U0` hunk: a contiguous stretch of changed lines with exact
  positions. `FileDiff.runs` keeps them exactly as git emitted them.
- A **block** is one or more runs merged when separated by at most `MERGE_GAP`
  (3) unchanged lines.
- A block over `--section-max` changed lines (48) is cut into **sections** of at
  least `--section-min` (12), packing whole runs first and preferring a seam the
  code already has: the `-`/`+` turn, a blank line, an unindented line, a
  definition. No parser and no language table.
- Each block's **`hunk_id`** is the first 16 hex digits of the SHA-256 of its
  changed lines with their `+`/`-` prefixes. Line numbers and context are not in
  it, so an edit elsewhere in the file, or a rebase, leaves the id alone, and an
  edited block simply gets a new id. Identical blocks in one file get `-2`,
  `-3`, … suffixes.
- Files with no runs (binary, mode-only, pure rename) get one whole-file block
  with `hunk_id` `*`. A submodule pointer change is followed by the submodule's
  own diff between the two pointers, one level deep, when it is checked out.

**Display rows** (`git.display_rows`) are replayed from the same runs plus the
new side's content: everything between two runs is unchanged by definition, and
deleted text is in the runs. There is never a second `git diff` for display,
because git's edit script differs between context widths and two scripts would
leave rows no block covers ([LOG, 2026-08-07](LOG.md#2026-08-07--display-rows-replayed-from-the--u0-runs)).
`verify_tagging` checks that every changed row is in exactly one block; if that
ever breaks, the file is still served, without block marks, and the error goes
to stderr.

## Snapshots and caches

`AppState.snapshot(mode)` returns a mode's `Snapshot` (file list plus a `key`
that changes whenever the diff does):

- **committed** is immutable for a given merge base and HEAD, so its key is
  `<merge base>-<HEAD>` and it is recomputed only when HEAD moves.
- **The other modes** include the index or the working tree, which change with
  no HEAD move. A fresh snapshot re-runs the `-U0` diff (cheap: its size follows
  the changed lines, not the files) and keys on a hash of the patch text,
  keeping the previous object when nothing changed.
- `/api/files` asks for a fresh snapshot (it is the client's poll); the other
  routes reuse the last one (`fresh=False`), so a file view and its block list
  can't disagree.

**File payloads** (rows, tokens, block ranges: everything in `/api/file` except
review state) are cached in memory by `(path, fingerprint)`. The fingerprint
hashes that file's patch text, and `--full-index` puts real blob ids in it (git
hashes working-tree content too), so it changes exactly when either side does.
That makes a payload valid for every mode it appears in; the cache is only pruned
to what live snapshots still reference. The client uses the same fingerprint
(`v`) to decide whether the open file needs reloading.

Committed payloads are also written to `~/.cache/seendiff/v2-<base>-<head>/`
(`XDG_CACHE_HOME`) and survive restarts. The `v2` prefix is the payload format:
bump it when a payload changes shape. Directories untouched for 30 days are
pruned at startup.

## Review state

`store.py` holds two tables in `~/.local/state/seendiff/state.db`
(`XDG_STATE_HOME`):

- `block_state(scope, path, hunk_id, seen_at, reviewed_at)`. A row older than 60
  days on both timestamps is pruned at startup.
- `prefs(key, value)`: UI preferences (theme, font, layout, sidebar width,
  walkthrough position and so on). Every run gets a fresh port and `localStorage`
  is per origin, so the server is the store: `/` inlines the saved prefs into the
  page (replacing `__PREFS__`) before first paint, and `localStorage` is only a
  write-through cache.

The **scope** is `repo | base_ref | branch` (the merge-base SHA on a detached
HEAD). It is symbolic on purpose: no commit SHA in it, so amending, rebasing or
rewording keeps the state, and invalidation is left entirely to the content
hashes. The mode isn't in it either, so a block reviewed under *unstaged* is
still reviewed once committed.

Two states per block:

- **seen** is automatic and advisory. The client works it out from scroll
  geometry, not `IntersectionObserver` (virtualized rows mostly have no DOM
  node): a block is seen after 500 ms fully in view, or once it has scrolled past
  the top, or at the end of the file, and only while the window has focus. Seen
  marks are queued and flushed every 2 s, and with `sendBeacon` when the page is
  hidden; `_json_body` accepts the beacon's body whatever its content type.
- **reviewed** is always explicit. Marking reviewed fills in `seen_at` too;
  un-reviewing returns a block to seen.

Progress bars weigh each block by its changed lines (so a two-line fix doesn't
count like a rewrite); the counters count blocks, since that's what the keys act
on.

## Syntax highlighting

Server-side, from Pygments' token stream rather than its HTML formatter: token
values are split at newlines before they are emitted, so no span ever crosses a
line. The old and new sides are lexed separately and mapped back onto rows
(context and added rows take new-side tokens, deleted rows old-side ones), so a
deleted line inside a vanished multi-line string still colours correctly.

`highlight.guard` skips files over 10,000 lines or 1 MB, or with a mean line
longer than 500 characters (minified code); `--no-highlight` turns it off. A
lexer failure never takes down a file view.

Each theme's palette is a Pygments style (`server.THEME_STYLES`) scoped under
`[data-theme="…"]`, generated at startup and inlined into the page, which is
served `no-store`. A theme's UI colours are CSS variables in `index.html`, and a
test checks that every theme has both halves.

## The client

`static/index.html` holds the whole UI:

- **Rows are always virtualized.** Row heights come from a prefix-sum offset
  table built from a fixed pixel line height and the measured glyph width (long
  lines wrap at exact column counts); geometry, the minimap, auto-seen and
  scrolling all read that table. A font switch re-measures once fonts have
  loaded.
- **A view layer** (`viewRows` / `viewOf`) maps payload lines to rendered rows,
  one per line in unified view and paired deletions/additions in side-by-side,
  so nothing else needs a per-layout branch.
- **Live refresh:** while the tab is visible it polls `/api/files` every 2 s,
  compares the raw response with the last one, and reloads the open file only
  when its fingerprint changed, keeping the scroll position.
- **Search** runs on the server (`/api/search`), because the page only holds the
  open file's payload; it goes through the same payload cache. In the open file,
  matches are marked by splitting tokens at match boundaries
  (`tests/marks.test.js` covers that code).

## Live connection and exit on close

Each tab holds one Server-Sent Events stream, `/api/events`, open for as long as
it lives. `server.Presence` counts those streams: the server is idle once it has
seen a tab, none is left, and 2 s have passed (enough for a reload to
reconnect). A closed tab drops its socket however the browser closes, which
`pagehide` and beacons don't guarantee.

`cli._supervise` runs beside uvicorn: when `Presence` is idle (and
`--keep-running` wasn't given) it stops the server, and on any shutdown it sets
`AppState.stopping` so open streams end themselves. uvicorn's graceful shutdown
is capped at one second for streams that don't.

The stream opens with a `hello` naming the server process. In the client:

- The stream failing covers the page with an inert overlay; still down after
  3 s, the page says seendiff has stopped and tries `window.close()` (browsers
  only allow that for windows a script opened, so usually the overlay stays).
- On reconnect, a `hello` from a different process (seendiff started again on
  the same port) reloads the page; the same process just lifts the overlay.

## Images

Files with an extension in `git.IMAGE_TYPES` (PNG, JPEG, GIF, WebP, BMP, ICO,
AVIF) have no display rows. Their payload carries an `image` entry with the type
and each side's size, and the client loads the two sides from
`/api/blob?path=&side=old|new`. That route serves only image extensions, refuses
sides over 50 MB, and sends `Content-Security-Policy: sandbox` and `nosniff`,
since it returns file bytes verbatim. The client puts the fingerprint in the URL,
so blobs are cached forever. An image is its file's single whole-file block.
SVG is left out on purpose: its source is what a reviewer reads.

## Committing and pushing

**Proposals** are `COMMIT_MSG_<description>.txt` files in the repo root
(`git.COMMIT_MSG_RE`), so several can wait at once and editors see them as plain
files. They are listed oldest first, and their `mtime_ns` goes to the client as a
string, since a JS number would round it.

| route | does |
|---|---|
| `GET /api/commit` | proposals, staged changes, counts, push status |
| `PUT /api/commit/message` | save an edited proposal |
| `POST /api/commit` | commit what's staged with a message, then delete its proposal |
| `POST /api/push` | push the current branch |

The three writing routes need the `X-Seendiff: 1` header. A page on another site
can make the browser POST to `127.0.0.1:<port>` with an acceptable `Host`, but it
can't add a custom header without a CORS preflight, which this server never
answers. Saving or committing a proposal whose file changed on disk since the
client read it fails with 409, so a stale copy is never written or committed.

`git.commit` runs `git commit --cleanup=strip -F -`: `#` lines are dropped, hooks
run, and a hook's failure text comes back to the panel.

`git.push_status` works out what a push would do from the local remote-tracking
refs (as fresh as the last fetch): the upstream, or `origin` (or the only remote)
for a branch without one; how many commits are ahead; and a reason it's blocked
(detached HEAD, no remote, several remotes and no upstream, or behind the
upstream). seendiff never force-pushes. `git.push` runs with no terminal
(`GIT_TERMINAL_PROMPT=0`, stdin closed, its own session) and a 180 s timeout, so
a credential prompt can't hang the request or appear in the terminal that started
seendiff.

**`git cm`** (`gitcm.py`) works from the same files: it picks a proposal, opens
it in the editor through `git commit`, and deletes it once committed.

## Walkthroughs

A walkthrough is a JSON script that an agent writes and seendiff plays: ordered
steps, grouped into acts, each pointing at a line range. The format and the
writing rules live in [the skill](skills/seendiff/SKILL.md#file-format).

- **Validation** (`walkthrough.py`) is complete and loud: every problem is
  collected, and a script with any problem doesn't load. It checks the schema,
  that steps keep act order, depth 0 or 1, at most 40 steps and 3 `also` refs,
  that `forward` names a later step, and every ref: the path resolves inside the
  repo (this is a file-read primitive fed by a JSON file), the lines exist, and a
  `hunk_id` names a block of the current diff. `--check-walkthrough` prints the
  same diagnostics and exits 0 or 1.
- **The empty walkthrough:** a missing or empty file, `{}`, `null` or a script
  with no steps is not an error. seendiff shows the plain diff and keeps
  watching. The skill relies on this: it starts one seendiff per conversation
  with `--walkthrough` before any script exists.
- **Reloading:** `AppState.load_walkthrough` reloads when the file's mtime moves
  and keeps the last good copy if the new one fails, reporting the error in the
  bar. `GET /api/walkthrough` answers the script, `{"empty": true}`, 422 for a
  broken script with no good copy, or 404 when seendiff was started without
  `--walkthrough`.
- **Dismiss** (`POST /api/walkthrough/dismiss`) truncates the file to `{}`, so
  every open tab drops it too.
- **Files outside the diff** that a script points at are served as all-context
  payloads (`git.worktree_rows`), cached by `(path, mtime)` since the diff caches
  only notice HEAD moving. They have no blocks and no review state.
- Playback position is a pref keyed by the script's digest, so a rewritten
  script starts fresh. Playing a walkthrough never marks anything reviewed.

## HTTP API

All JSON unless noted. Routes that read a diff take an optional `?mode=`.

| route | purpose |
|---|---|
| `GET /` | the page, with prefs and the syntax palette inlined |
| `GET /api/events` | SSE stream: presence and server identity |
| `GET /api/files` | file list, counts and review totals for a mode (polled) |
| `GET /api/file?path=` | one file's rows, tokens and blocks with their state |
| `GET /api/blob?path=&side=` | one side of an image, raw bytes |
| `GET /api/search?q=` | content search across the diff (`regex`, `case`, `limit`) |
| `POST /api/seen` | batch of seen blocks |
| `POST /api/reviewed`, `DELETE /api/reviewed` | mark / unmark reviewed (block, file, all) |
| `DELETE /api/state` | wipe this scope's seen and reviewed state |
| `GET`/`POST /api/prefs` | UI preferences |
| `GET /api/walkthrough`, `POST /api/walkthrough/dismiss` | see [Walkthroughs](#walkthroughs) |
| `/api/commit*`, `/api/push` | see [Committing and pushing](#committing-and-pushing) |
| `GET /fonts/{name}`, `GET /theme.css` | static assets |

## Build, run and test

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest httpx
.venv/bin/python -m pytest tests/ -q
.venv/bin/seendiff                      # run from the checkout, in any repo
```

The tests build scratch git repositories and drive the app through FastAPI's
test client. `tests/test_marks_js.py` runs `tests/marks.test.js` under `node`
(skipped without it); it extracts the functions it tests from `index.html` by
name, so it can't drift from what ships.

To keep a development run away from your real review state, point
`XDG_STATE_HOME` and `XDG_CACHE_HOME` somewhere else. `python -m seendiff.git`
prints the blocks of the current repo's committed diff, which helps when
debugging block identity.

## Packaging and the website

`packaging/arch/PKGBUILD` builds `seendiff-local`, a personal package of this
fork, from the **committed** HEAD of the checkout it sits in (`git archive`;
uncommitted changes only get a warning). It provides and conflicts with
`seendiff` and the AUR's `seendiff-git`, which builds upstream. Its version is
`<pyproject version>.r<commit count>.g<short sha>`, computed at the top of the
file because makepkg writes a `pkgver()` result back into the tracked PKGBUILD.
`prepare()` drops the exact Pygments pin to follow the system package, `check()`
runs the test suite, and `package()` installs both console scripts, the licences,
the README, the commit rule, and the skill under `/usr/share/seendiff/skills/`;
`seendiff-local.install` tells the user how to copy the skill into
`~/.claude/skills/`.

```bash
cd packaging/arch && makepkg -si
```

`docs/` is the static landing page published at seendiff.com (with `CNAME` and
`.nojekyll`). `docs/img/logo/generate.py` renders the logo set.

## Planned

**Conversations anchored to diff lines.** Click a line, ask a question, and get
answers from a real Claude Code agent working in the repo, not a chat wrapper:

- One Claude Agent SDK session per thread, pooled and reaped when idle, torn
  down on shutdown, with `cwd` set to the repo. A thread's first message is a
  generated preamble (path, line, surrounding hunk) followed by the question;
  the agent reads the rest itself.
- Version one is read-only (Read, Grep, Glob), one thread per line, on a cheap
  model. Answers and tool activity stream over SSE. Threads live in SQLite keyed
  by `(path, hunk_id, line)`, with a badge per line.
- The agent's subprocess environment drops `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN`, which would silently switch it from the subscription
  to metered billing ([LOG, 2026-08-02](LOG.md#2026-08-02--line-anchored-agent-chat-designed)),
  and the panel shows the auth status `claude auth status --json` reports.
- Later: edits behind a per-tool approval prompt, refreshing the diff after an
  edit, threads over several lines or a whole block.
- Open: hunk ids change when a block's content does, so should a thread follow
  its block or stay where it was asked? The SDK's class and option names are
  unverified against the installed package.
