# seendiff log

Decisions, findings and rejected alternatives, dated, oldest first. Entries are
added, not rewritten; a later entry corrects an earlier one. What is true now is
in [DEVELOPMENT.md](DEVELOPMENT.md); user-visible changes are the commit
messages.

Entries up to 2026-08-25 are from upstream (acme-dot-bot/seendiff), condensed
from its design documents and commit messages. Later entries are this fork's.

## 2026-07-31 — The v0.1 build brief

seendiff was built from a written brief for reviewing your own stack of commits
before pushing. Existing tools fell short: Meld, kdiff3 and the like forget your
progress when the window closes; `git difftool` runs once per file pair; forge
UIs have the "viewed" checkbox but need a pushed branch and a PR, and show hunks
instead of whole files; `git range-diff` keeps no state.

Decisions the brief locked:

- **Committed changes only**, so the diff for a given merge base and HEAD is
  immutable and caches forever. Uncommitted work was to be listed in a banner,
  not shown. File watching and live reload were out of scope.
- **Base** `origin/master` → `origin/main` → `master` → `main`, resolved with
  `rev-parse --verify`; an explicit `git merge-base` call (three-dot semantics)
  rather than `diff --merge-base`, which would need git ≥ 2.30, and the SHA was
  wanted anyway.
- **Whole files, unified view**: line refs need real file numbering, and
  side-by-side seemed to leave no width beside the minimap.
- **Two review states**, seen (automatic) and reviewed (explicit).
- **Symbolic scope key** `repo|base|branch`, so state survives a rebase, with all
  invalidation left to content hashes. Called the most important decision.
- **Block identity** is a content hash of the changed lines from a `-U0` pass,
  blocks merged across gaps of up to 3 lines.
- **Server-side Pygments token stream**: highlight.js and Prism emit spans that
  cross line boundaries, and Shiki needs WASM and a bundler.
- **One HTML file, no build step**; fetch never implicit.
- **Git as a subprocess**, never `git difftool` or `GIT_EXTERNAL_DIFF`, which
  invert control and run once per file pair.

Smaller calls: auto-seen from scroll arithmetic, not `IntersectionObserver`,
because virtualized rows have no DOM nodes; a ref to deleted lines uses the
preceding new-side line number, since the LLM reads the working tree. An early
draft used `-U3` for identity and mapped git's hunks onto the display; it was
dropped as the riskiest code in the project.

Deferred: a ref basket for collecting refs across files, word-level diff (it
competes with syntax colour for the same visual channel), conflicted files,
comments, per-commit attribution, multi-repo views.

## 2026-07-31 — Side-by-side view after all

Added a view layer (`viewRows` / `viewOf`) so unified and side-by-side share all
geometry, overriding the brief's unified-only call. It surfaced two geometry
bugs: the glyph-width probe soft-wrapped and measured the pane instead of a
glyph, and the scrollbar appearing after the spacer was sized shrank the width
nothing recomputed. Known gap at the time: below about 600 px wide, the wrap
estimate drifts by a line.

## 2026-08-01 — Walkthroughs designed and built

The design: an agent writes a walkthrough as a JSON file, seendiff plays it in a
bar under the code, and `←`/`→` step through it. Decisions:

- A file passed on the command line: no socket and no agent dependency inside
  seendiff.
- Refs are new-side line ranges, the same numbering as copied refs. No drift
  handling: a ref that doesn't resolve fails at load; regenerate, don't
  reconcile.
- Files outside the diff can be shown, and an empty diff still plays, because
  "explain this code" usually has no diff.
- Two axes of layering: acts (ordered chapters) and depth (skippable detail).
- Playing a walkthrough never marks anything reviewed: an explanation is not a
  sign-off.
- Progress lives with the client, keyed by a hash of the script, not in the
  review database.
- Validation fails loudly, plus `--check-walkthrough`, because the input is
  machine-written.
- Kept apart from any agent chat: a walkthrough stays a static, diffable file.

The two open questions settled as the brief leaned: `w` leaves walkthrough mode
(`Esc` already closes dialogs), and detail steps are shown by default. Out of
scope: authoring in the UI, autoplay, audio, export, multi-repo walkthroughs.

## 2026-08-01 — Preferences move to the server

Every run binds a fresh port and `localStorage` is per origin, so each launch
forgot the theme. Prefs went into the state database and are inlined into the
page; `localStorage` became a write-through cache.

## 2026-08-02 — The empty walkthrough

The skill became the general "show me code" skill: it starts one seendiff per
conversation, always with `--walkthrough`, before any script exists. So a
missing, empty or `{}` file became a state (plain diff, keep watching) rather
than a load error, while half-written JSON stays an error. Dismissing truncates
the file to `{}` instead of hiding the bar, so it sticks across tabs and the
agent's next script replaces it.

Search arrived at the same time. The browser's find can't see virtualized rows,
and the page holds only one file, so search runs on the server.

## 2026-08-02 — Line-anchored agent chat designed

Designed, not built: conversations about a diff line, answered by a Claude Agent
SDK session with the repo as its working directory. Calling the Messages API
with hand-rolled tools was rejected as reimplementing what Claude Code already
does, and as metered billing.

Finding, verified with `claude auth status --json`: `ANTHROPIC_API_KEY` in the
environment silently overrides an OAuth subscription login and switches to
metered billing. `authMethod` reads `"claude.ai"` either way; the reliable
signals are `apiKeySource` (present only with a key), and `subscriptionType`,
`email` and `orgName` (null with a key). `ANTHROPIC_AUTH_TOKEN` does the same.
Hence the plan to strip both from the agent's environment. The plan itself is in
[DEVELOPMENT.md](DEVELOPMENT.md#planned).

## 2026-08-02 — The syntax palette is inlined

Served as `/theme.css` with a day-long cache, a renamed theme left browsers on a
stale sheet whose selectors matched nothing, and the theme lost its colours. The
palette is now inlined into the page, which is `no-store`. Solarized split into
dark and light the same day.

## 2026-08-07 — Sections and weighted progress

A block had no upper size, so a rewritten file was one checkbox for two thousand
lines. Oversized blocks are now cut into sections at seams the code already has,
with no parser or language table, and section ids stay content-derived. The
progress bar now weighs blocks by changed lines, since a two-line fix and a
rewrite each counted as one block.

Two older bugs surfaced: auto-seen could never fire for a file's last block or
for a block taller than the pane, so such files never reached 100%; and search
returned 500 on binary files because decoding a PNG as UTF-8 threw, which led to
all git output being decoded with replacement.

## 2026-08-07 — Display rows replayed from the -U0 runs

Display rows used to come from a second `git diff --unified=999999`, matched to
blocks by line number. Git's edit script is not stable across context widths: on
a real change, a `)` line identical on both sides was context under `-U0` and a
delete/add pair at full context (52 changed lines against 54). The two extra rows
fell in no block, and the invariant check failed the whole file view with a 500.
Rows are now replayed from the `-U0` runs and the new side's content, so there is
one edit script. An invariant failure since then logs and serves the file without
block marks.

## 2026-11-18 — Uncommitted work, live, and committing from the review

This fork reverses the brief's "committed only" and "no live reload". With
agents editing the working tree, the diff a reviewer wants includes uncommitted
work, and it moves while the tab is open. Four modes (all, committed, staged,
unstaged), with `all` the default; the tab polls every 2 s.

- Untracked files are diffed through a temporary copy of the index with them
  marked intent-to-add, rather than synthesized patches, whose binary detection,
  newline markers and hashes would drift from what git emits once they're added.
  The copy keeps the index's mtime: with a fresh one, git's racy-git check is off
  and a same-size edit made just after `git add` reads as unchanged.
- `--full-index` puts blob ids in the patch text, so one hash of a file's patch
  identifies both sides and keys a payload cache that is valid across modes.
- The review scope stays mode-independent, so review carries from unstaged to
  staged to committed.
- Committing: proposed messages are `COMMIT_MSG_<description>.txt` files in the
  repo root, so several can queue and any editor can open them. `git cm` reads
  the same files. Repo-changing routes require an `X-Seendiff` header as a CSRF
  guard.

## 2026-11-21 — Exit with the last tab; images

The server now exits when its last tab closes. Presence is an open SSE stream
per tab rather than `pagehide` or beacons, because some embedded browsers never
fire `pagehide`, while a closed tab always drops its socket. Images got a
before/after view; SVG stays a text diff because its source is what gets
reviewed.

## 2026-11-24 — Stale tabs

A tab whose server is gone is covered and made inert. Scripts can only close
windows they opened, so `window.close()` mostly does nothing and the cover is
what the user sees. Each server sends an instance id, so a tab reconnecting to a
new seendiff on the same port reloads instead of acting on old state. uvicorn's
graceful shutdown is capped at one second, because open streams would otherwise
make Ctrl-C wait.

## 2026-11-24 — Push from the commit panel

Push shows what would go where before it runs. It never force-pushes: a branch
behind its upstream is blocked with "pull first" rather than left for git to
reject. It runs with no terminal, so a credential prompt can't hang the request
or appear in whichever terminal started seendiff; SSH agents and credential
helpers still work. `#` lines in a proposal are notes to the reviewer, shown
above the message and stripped on commit.

## 2026-11-24 — Documentation reorganised

The docs were split three ways: README.md for users, DEVELOPMENT.md for how
seendiff works now, and this log for history. The upstream design documents
PLAN.md, CODEWALKTHROUGH.md and CHAT-SUPPORT.md were condensed into the entries
above and removed. MARKETING.md, an outline for the landing page, was dropped
because the page in `docs/` replaced it. Git history keeps the originals.
