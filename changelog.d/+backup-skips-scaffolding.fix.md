# a teardown with nothing to lose writes no tarball, and the ones it writes get their own directory

#805 stopped `remove-worktree` archiving `copies` and `gitignore_additions`, but most lexray
teardowns still wrote an archive holding nothing anybody would open: three symlinks back into
the main checkout (`.claude/rules`, `.claude/settings.local.json`, `data`) plus the `.env` and
`CLAUDE.local.md` that `create-worktree` had generated and nobody had touched. Each one landed
loose in the checkouts' parent directory, a couple of hundred of them among the worktrees.

An untracked or ignored symlink that resolves into the main checkout is now left out: it holds
no bytes of its own. `create-worktree` records the sha256 of the `.env` and `CLAUDE.local.md` it
writes, in the worktree's own git dir, and the teardown leaves them out while they still match.
An edited one is archived, which is why #805 kept `.env` at all. A tree made before the stamp
falls back to mtime against `.worktree-port`; with neither clue the file is archived as before.
A teardown that finds only scaffolding says "No backup needed", and archives now go in
`worktree-backups/` beside the checkouts.
