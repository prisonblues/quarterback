# a worktree could be given files but never told to build anything

`symlinks` and `copies` were the only two levers a project had over what a new worktree
contains, and neither builds. So a Python project whose worktrees should each have their own
virtualenv had no way to ask for one, and every worktree symlinked the main checkout's — one
mutable dependency set behind N branches, where whichever worktree last ran `uv sync` decides
what all of them have installed, and nothing warns.

Measured in a consuming repo: 44 worktrees on one venv across 4 distinct `uv.lock` files and 5
distinct `pyproject.toml` files, with one branch carrying four packages another lacked. A sync
from the second would strip four packages the first imports.

`.worktree.json` now takes a `setup` array — shell commands run **in** the new worktree, after
symlinks and copies so the files are where they will finally be, and before Docker so an image
build can use what they produced. Nothing in it is Python-specific.

```json
"setup": ["uv sync --frozen"]
```

### A failed setup command is loud, and not fatal

The worktree exists by the time these run, so aborting would lose the branch and the port along
with the mistake, and skip the port, nginx and summary steps that still have work to do. Under
`set -euo pipefail` that has to be written rather than assumed.

It must not pass quietly either: an absent `.venv` surfaces much later as a pre-push refusal or
an import error with no obvious cause. So the failure is named where it happens, repeated in the
closing summary, and printed with the command to re-run.

Each command gets its own subshell, so a `cd` in one does not relocate the next.
