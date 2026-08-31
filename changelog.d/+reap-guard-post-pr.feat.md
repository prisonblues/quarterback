# nothing tore a worktree down when its PR landed, and reaping one could take commits with it

Per-worktree stacks piled up because no step ran at the moment they became garbage.
`create-worktree` starts a stack per worktree — for one repo an app container plus a
consumer with four workers, 1–2 GB a pair — and every workflow that says
"`/drop-worktree` when the PR merges" is addressing a session that has usually ended
by the time the merge happens. So the instruction landed on nobody: 45 containers
holding 16.7 GB accumulated on one machine and the kernel OOM killer took the
compositor.

`fix-and-land` now tears the worktree down after a merge. It is the one path that
lands work with nobody in the room, and it had no teardown step at all.

`remove-worktree` now refuses a branch carrying commits its PR never took **and that
no remote has**. Deleting the branch is the step that loses work, and the commits most
at risk are the ones added *after* the PR — the post-merge tweak that exists in exactly
one place. Three were found sitting in reapable worktrees.

### The waterline is the PR's head SHA, not the default branch

`git branch --merged main` is only meaningful where PRs target the default branch. One
repo here merges into `fca` and `test` while `main` sits frozen months back — there that
check calls *every* branch unmerged. The PR's own head SHA is base-independent: anything
past it is work the PR never carried.

`--not --remotes` keeps this about loss rather than tidiness. Only the *local* branch is
deleted, so a post-PR commit already pushed somewhere survives the teardown; refusing
those would block the reaping this guard exists alongside, for work in no danger. A stale
remote-tracking ref makes it over-refuse, which is the safe direction.

### Advisory by construction

No `gh`, no PR, or a head object not held locally all mean "cannot tell", and cannot-tell
never blocks a teardown. `--force` and `--keep-branch` both skip it, the latter because a
kept branch keeps its commits.

The `git cat-file -e` probe is load-bearing: with the object absent, `git log a..b` fails
and a bare `| wc -l` reads 0 — "nothing to lose" on exactly the branch that cannot be
vouched for.
