"""Hand each test a COPY of a fixture repository built once, instead of rebuilding it (#800).

Four suites under `harness/tests` were spending between 45% and 62% of their CPU
in fixture setup, all of it the same shape: a `git init`, a bare remote, a clone,
a commit and a push, rebuilt from scratch for every test. `test_pre_push_hook.py`
was paying ten `git` processes and a `qb-hooks install` per test, 54 times, for a
repository not one of its tests needs to have been made freshly — only to have one
of its own.

## Why a copy and not a shared repository

Because the repository is a per-test INPUT, not a constant. Every one of these
suites pushes to its remote, commits on its branch, rewrites its config or
installs hooks into it. #785 established what happens when that distinction is
missed: `test_qb_seats.py` looks like exactly the same opportunity — 168 tests
each doing a `git init` — and session-scoping it would have been silently wrong,
because three of its tests write a stub into a directory that is on PATH and tmux
freezes its environment at server start, so every later test would have run
against the FIRST test's PATH while continuing to pass.

So what is shared here is the BUILDING and never the state. The expensive artefact
is built once at module scope and copied per test, which is the pattern #800 names
as the safe one.

## The one thing a copy gets wrong, and what this module does about it

A git repository records where it lives and where it came from — `remote.origin.
url` and `core.hooksPath` in `.git/config`, and the `clone: from …` line each
reflog carries. Copied verbatim, every test's repository would still point at the
TEMPLATE's bare remote, so the first test's `git push` would land there and the
second test would find the first one's commits waiting for it. That is a suite
that passes for the wrong reason, which is the failure this whole exercise is
under orders not to reintroduce.

`copy()` therefore repoints every such reference as part of the copy, and does it
by rewriting the template's own path wherever it appears rather than by naming the
files that are known to carry it today — a list would go stale the first time git
or `qb-hooks` recorded a path somewhere new, and it would go stale silently.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def hermetic_env(where: Path, **extra: str) -> dict[str, str]:
    """The environment the `_hermetic_git` fixtures set, for a fixture too wide to use one.

    Those fixtures are function-scoped and monkeypatch-based, and pytest does not
    promise to run one before a module-scoped fixture that a test also asks for —
    so a template builder cannot rely on having been given their environment and
    has to state it. Without it the template inherits this host's global
    `core.hooksPath` (a nix store path holding a gitleaks `pre-commit`) for the
    commits it makes, which is the host-dependence those fixtures exist to end.

    `where` is where the two config files are pointed, and it must be OUTSIDE the
    directory `copy()` is going to hand out: that function carries every child of
    the template across, and a `gitconfig` among them would land in each test's
    `tmp_path` — which is exactly the path `_hermetic_git` points
    `GIT_CONFIG_GLOBAL` at. Every template below therefore builds one level down
    from the directory named here.
    """
    return {**os.environ,
            "GIT_CONFIG_GLOBAL": str(where / "gitconfig"),
            "GIT_CONFIG_SYSTEM": str(where / "gitconfig-system"),
            **extra}


def copy(template: Path, into: Path) -> None:
    """Copy everything in `template` into `into`, repointed at its new location.

    `template` is the DIRECTORY the fixture was built in — not one repository
    inside it — because the references being rewritten cross between its members:
    a checkout names the bare remote beside it. Copying the pair and rewriting the
    root that both paths start with is what keeps them pointing at each other.

    Symlinks are preserved rather than followed: `qb-hooks install` re-exports a
    machine's managed hooks as symlinks to its own forwarder, and dereferencing
    those would replace each forwarder with a copy of the script it forwards to —
    a fixture that no longer holds the arrangement its tests are about.
    """
    for member in sorted(template.iterdir()):
        shutil.copytree(member, into / member.name, symlinks=True)
    repoint(into, was=template, now=into)


def repoint(tree: Path, *, was: Path, now: Path) -> None:
    """Rewrite `was` to `now` in every file under `tree` that names it.

    Every file, rather than `.git/config` and the reflogs that carry it today: a
    path git records somewhere this module has not heard of is a pointer back at
    the shared template, and the whole point of the copy is that no such pointer
    survives it. Rewriting on the byte string is the same reason — a file that
    is not text still gets repointed rather than skipped.

    Objects and packs are the exception, and an ASSERTION rather than a skip.
    Their contents are zlib-compressed, so the template's path cannot appear in
    one; if it ever does, the file is not what this function thinks it is, and
    substituting a different-length path into it would corrupt the object store
    quietly. Better to stop.
    """
    marker, replacement = str(was).encode(), str(now).encode()
    for path in tree.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        blob = path.read_bytes()
        if marker not in blob:
            continue
        assert "/objects/" not in path.as_posix(), (
            f"{path} names the template it was copied from, and it is a git object — "
            f"rewriting it would corrupt the object store rather than repoint the repo")
        path.write_bytes(blob.replace(marker, replacement))
