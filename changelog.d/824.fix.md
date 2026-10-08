# prune-worktrees sweeps the bytecode caches worktrees leave behind

With `PYTHONPYCACHEPREFIX` set, Python keeps every checkout's `.pyc` files under the prefix,
keyed by the source's absolute path. Removing a worktree removed the sources and left the
bytecode, and no category here looked there — so a box that churns worktrees filled its disk
(183 dead checkouts, ~40G) while the sweep reported `Nothing to prune. Clean.`

A seventh category, `Orphan bytecode caches`, lists cache directories under
`$PYTHONPYCACHEPREFIX<parent>` named `<project>` or `<project>-*` whose directory no longer
exists, and `--remove-pycache` deletes them. A directory that still exists, registered or not,
keeps its cache; another project's is never considered. Bytecode regenerates on import, so
this is the one category whose removal costs nothing but a slower first run.
