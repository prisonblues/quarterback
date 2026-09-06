# a symlinked CLAUDE.md looked fine and could not be edited

The tracked-path guard in the symlink step tested `-d`, so it only ever covered
directories. A tracked *file* in the symlink list fell through to the wholesale branch,
which destroys git's checkout of the path and links to the main repo instead.
`CLAUDE.md` is in the default list, and plenty of repos commit one.

The comment above that guard already makes the whole argument — git owns those paths in
the worktree, and writing over them is not ours to do — and every word of it is true of
files, with one fewer thing to get right, since a file has no untracked entries inside it
to share. The fix is to leave git's checkout alone.

What made this recur rather than get noticed is that the damage is invisible. The type
change (`T`) is hidden by the skip-worktree pass at the end of setup, so the file reads as
normal in `git status` and is not: every edit from the worktree writes **through the link
into the main checkout**, landing on whatever branch that has out, and cannot be committed
from the worktree at all. An agent told to update `CLAUDE.md` from a worktree therefore
either scribbles on the main checkout or gives up and files the content somewhere worse.
Both have happened, more than once, which is what prompted this.

Verified against a repo that does track it. Before: a symlink, `T` in `git status`, `S`
from `git ls-files -v`, edits landing in the main checkout. After: a regular file, `H`, a
clean status, and an edit showing as ` M` in the worktree with the main checkout
untouched. Repos that do not track the file are unaffected — `tracked_in_main` is false
for them and the wholesale symlink still applies, so sharing one `CLAUDE.md` across
worktrees survives everywhere it was actually wanted.

### Still open: a docker failure skips the quieting step

`create-worktree` exits 1 on a failed `docker compose up` *before* "Quiet the git state"
runs, so a worktree whose containers do not start is left visibly dirty on `.gitignore`,
`docker-compose.yml` and any wholesale symlinks. The script prints "The worktree exists
but its containers are not running… Retry: docker compose up -d", so it means to leave a
usable worktree; it should finish the steps that do not depend on docker before it bails.
Left alone here because it needs a decision about the nginx steps that also follow.
