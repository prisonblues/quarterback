# Loops — Dependabot Lander

@description Dependabot CI-green auto-lander: classify bumps, gate on CI, merge patch/minor, fix red CI in a worktree.
@arguments $ARGS: [repo] [--execute]   (repo defaults to the cwd's repo)

Run the dependabot lander for a repo.

1. Parse `$ARGS`: optional **repo** (default: the cwd's repo); note whether `--execute` was given.
2. **Default is DRY-RUN.** Run (from anywhere):
   ```
   python3 ~/.claude/loops/lander.py                          # add --repo <path|name> for another repo
   ```
   and show the user the plan (per-PR class, CI status, and the action it would take).
3. If the user explicitly asked to `--execute`: this **merges PRs / pushes fix commits to a real
   repo** (outward-facing, hard to reverse). **Confirm with the user first**, then run:
   ```
   python3 ~/.claude/loops/lander.py --execute
   ```

Policy lives in the repo's own `.harness-rules` (`auto_merge`): only dependabot patch/minor auto-merge on green;
security bumps escalate; red CI opens an edit-only fix in a worktree. Never merges anything else.
