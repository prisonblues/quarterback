# quarterback's own worktrees all shared one virtualenv, and a config could not say otherwise

The harness gained a `setup` hook so a project's worktrees could build their own environment,
and quarterback was still symlinking `.venv` like everyone else. Measured here on 2026-09-07:
**24 worktrees on one venv across 4 distinct `pyproject.toml` files**, with the editable
`.pth` pointing at `quarterback-fix-issue-743-lock` — so the *main* checkout was importing
another branch's code, and nothing said so.

This repo keeps no `uv.lock` (it is gitignored, deliberately), which makes a shared venv worse
rather than better: two worktrees can resolve different versions and the last one to install
wins for everybody.

`.worktree.json` now declares an empty `symlinks` list and builds the venv with the two
commands `README.md` already documents for a fresh checkout.

### `"symlinks": []` now means "symlink nothing"

It did not before, and that is why this is a fix rather than a config edit. `cfg_array` yields
nothing for a key that is **absent** and nothing for one set to `[]`, so the defaulting could
not tell them apart and applied `[.venv, .claude, CLAUDE.md]` to both. A repo that deliberately
removed the shared `.venv` got it symlinked straight back, silently, and the only visible
symptom was one venv serving every worktree.

The defaulting now asks whether the key is present (`cfg_has`) rather than whether it produced
anything. An absent key still gets the defaults, so nothing that never mentioned `symlinks`
changes.

### Existing worktrees need a one-off

`.worktree.json` only shapes worktrees created after it changed. From an existing worktree's
root:

```bash
rm .venv && uv venv --python 3.12 .venv && uv pip install -e '.[dev]'
```

`rm` on a symlink removes the link, not the main checkout's venv.
