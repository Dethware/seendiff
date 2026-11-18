## Commits

I review and commit myself. Commit messages in my projects become user-facing
changelog entries.

- Never commit, amend or push unless I ask.
- **Stage your own changes** as soon as a change is ready: `git add` exactly
  what you changed. Staged is your work; unstaged is mine on top (fixes,
  suggestions), and that split is how I review. So:
  - Never stage changes you didn't make. If a file you changed also has edits
    from me or another session, stage only your hunks (write them as a patch
    and `git apply --cached` it) or leave the file unstaged and say so.
  - Never unstage anything.
  - If something else is already staged (an earlier change waiting for its
    commit), don't stage on top of it: leave yours unstaged, write its
    proposal anyway, and say it's queued behind the staged one.
- Propose the message in a file named `COMMIT_MSG_<description>.txt` in the
  root of the repository the change belongs to. `<description>` is 2–4 words
  joined by underscores, e.g. `COMMIT_MSG_fix_staged_diff.txt`. One file per
  change. Revise by overwriting your own file; never edit or delete a
  COMMIT_MSG file you didn't write, and never stage one.
- The message is one short line (≤ 72 characters) saying what changed for the
  user, as a changelog entry would. No implementation details, no file lists,
  no body unless I ask. Lines starting with `#` are dropped on commit; use them
  for notes to me.
- In your reply, paste the message as a code block and link the file by its
  absolute path. When you touched several repositories, give one file per
  repository and say which repo each is for.
- I commit with `c` in seendiff or `git cm` in the repo; both commit what's
  staged and delete the proposal file afterwards.
