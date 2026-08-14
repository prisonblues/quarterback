# Drop worktree — destroy this session's worktree, keep the branch

@description Tear down the worktree this session is working in and all its trappings — Docker containers, nginx block, isolated DB, port entry, directory — but KEEP the branch. Blocks if the tree is dirty. Clears the session's statusline marker.

Thin, safe driver over `remove-worktree --keep-branch`. The script does the
actual teardown (containers, nginx + restart, DB drop, port prune, dir removal);
this skill picks the right worktree, guards the preconditions, and clears the
session marker. You keep the branch (and its commits / any open PR); you lose
only the worktree and its runtime scaffolding.

## 1. Find the worktree this session owns

The shell cwd resets to the launch dir between tool calls, so you can't rely on
`git rev-parse --show-toplevel` to tell which worktree you're "in" — it'll just
report the main checkout. Instead read the **session marker** that `/fix-issue`
wrote when it provisioned the worktree:
```bash
MARKER="$HOME/.cache/claude-code/session-cwd/$CLAUDE_CODE_SESSION_ID"
WT_DIR=$(cat "$MARKER" 2>/dev/null)
```
- If `WT_DIR` is set and is a directory, that's the worktree to drop.
- If the marker is missing/empty (this session never entered a worktree), don't
  guess — run `git worktree list`, show the user the linked worktrees, and ask
  (AskUserQuestion) which one to drop (or confirm there's nothing to do). Set
  `WT_DIR` to their choice.
- Guard: if `WT_DIR` resolves to the **main checkout**
  (`git rev-parse --show-toplevel` from a fresh shell), STOP — there's no
  worktree to drop.

Record the branch (`git -C "$WT_DIR" branch --show-current`) and the
**create-name** (`basename "$WT_DIR"` with the `<project>-` prefix stripped).

## 2. Block if dirty

Check the worktree explicitly (cwd is not it): `git -C "$WT_DIR" status --porcelain`.
- **Any output → STOP.** The worktree has uncommitted or untracked changes;
  removing it would discard them (only a best-effort tarball backup is kept).
  Show the user the dirty files and tell them to commit, stash, or discard first,
  then re-run. Do **not** proceed automatically.
- Also check for **unpushed commits**
  (`git -C "$WT_DIR" log --oneline @{u}.. 2>/dev/null`): the branch is kept so
  these aren't lost, but warn if the branch has never been pushed (its only copy
  is local) before tearing down.
- Only proceed past a dirty tree if the user, having seen the list, explicitly
  tells you to discard it.

## 3. Destroy the worktree, keep the branch

`remove-worktree` operates from the create-name, so run it from anywhere (a fresh
shell is already at the main checkout, not inside the worktree — which is exactly
what `git worktree remove` needs):
```bash
remove-worktree --keep-branch "$CREATE_NAME"
```
Show the command before running it. `--keep-branch` preserves the branch; the
script removes the container(s), strips the nginx block and restarts nginx, drops
the isolated DB, prunes the `.worktree-ports` entry, and deletes the directory.
(If the user also wants the DB kept, add `--keep-db`.)

## 4. Clear the session marker

Remove the marker so the statusline stops pointing at the now-deleted worktree
and falls back to the main checkout:
```bash
rm -f "$HOME/.cache/claude-code/session-cwd/$CLAUDE_CODE_SESSION_ID"
```
(Even if you forget, the statusline self-heals — it drops a marker whose worktree
no longer exists — but clear it now so the next render is correct immediately.)

## 5. Report

Confirm to the user:
- the worktree directory is gone and its trappings (containers, nginx block, DB,
  port) were cleaned,
- the **branch `<name>` survives** (with its commits / PR intact),
- the statusline marker is cleared, so the bar reflects the main checkout again.

If `remove-worktree` reported anything it couldn't clean (e.g. a root-owned dir,
a container it couldn't stop), relay that verbatim and suggest `/tree-shake` to
sweep the remainder.
