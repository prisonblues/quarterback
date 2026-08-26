"""A `PATH` a suite controls completely, so that "the tool is absent" is absent.

Several suites under `harness/tests` drive a bash stanza lifted out of
`create-worktree`, `remove-worktree` or `prune-worktrees` and ask what it does
when a `qb-*` tool **is not installed on this host**. That is a real deployment
state and it is the one branch those tests exist to cover, so it has to be
arranged rather than hoped for.

They arranged it by building `PATH` as the stub directory plus
`os.path.dirname(shutil.which("bash"))` — bash's own directory, added because the
stanzas shell out to `git`, `jq` and `tr` and those have to come from somewhere.
On a machine where the harness is actually installed that directory is
`/etc/profiles/per-user/rich/bin`, which is **the directory the harness installs
into**: it holds `bash`, `git` and `jq`, and it holds `qb-claim`, `qb-release`
and `qb-admit` next to them. So the "absent" case handed the stanza the
production tool, and the three tests that assert on the absent branch failed on
every host where the harness works, while a fourth passed without ever taking
the branch it is named for (#385, #472). The comment those suites carried named
that exact hazard; the mechanism it chose was the one that fails.

The fix is not a longer `PATH` with the bad directory removed — the next tool to
reach a profile directory would put it straight back. It is a `PATH` built out of
things this module put there:

  * the caller's own stub directory, holding only what the test wrote, and
  * `toolbox()`, a directory of symlinks to the **named binaries** the stanza
    needs — resolved one file at a time, never a whole directory.

`sandbox_path()` then refuses to return a `PATH` on which any tool the harness
ships resolves, so the property holds at every call site rather than at the one
where somebody remembered to check it. A suite that later needs `sed` adds
`"sed"` to its `tools` and stays honest.

**PATH is only half of it.** Each of these stanzas falls back to
`${0%/*}/qb-<tool>` when `command -v` finds nothing — a sibling of the running
script. Under `bash -c` that `$0` is the interpreter's own path, so on this same
host the fallback reaches the same profile directory the `PATH` leak did. A suite
asserting the absent case must therefore ALSO run its stanza as a script file in
a directory it owns. `sibling_dir()` is where to put it, and it is checked the
same way.

Run: pytest harness/tests
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Iterable

#: The harness's own bin/, which is the authoritative list of names a sandbox
#: PATH must not resolve. Read as a directory rather than written out here, so a
#: tool added tomorrow is guarded the day it is added — the way #385 puts it, the
#: class grows with every future `qb-*` that gains a "what if this is missing"
#: test.
BIN = Path(__file__).resolve().parents[1] / "bin"


class ToolMissing(RuntimeError):
    """A binary a stanza needs is not on this host at all."""


def harness_tools() -> list[str]:
    """Every command `harness/bin` ships, by name.

    Asserted non-empty rather than allowed to be: a guard built from a glob that
    found nothing passes everything, which is the shape of failure this whole
    module exists to remove.
    """
    assert BIN.is_dir(), (
        f"{BIN} is not here, so the absent-tool guard below would pass everything. "
        "If a sandbox runs these suites without harness/bin, it has to copy it in "
        "— an inert guard in the sandbox it protects is no guard (#163).")
    names = sorted(p.name for p in BIN.iterdir()
                   if p.is_file() and not p.name.endswith(".py"))
    assert names, f"{BIN} holds no commands"
    return names


def toolbox(tmp_path: Path, tools: Iterable[str] = ()) -> Path:
    """A directory holding symlinks to exactly `tools` and nothing else.

    `python3` is the interpreter running this suite rather than whatever `PATH`
    resolves, for the reason `test_create_worktree_claim.py` already carried: the
    rollback goes THROUGH qbdata's own client and needs a real one, and a suite
    invoked as `.venv/bin/python -m pytest` — or run with the profile directory
    stripped, which is how a developer checks this very class of bug — may have
    no `python3` on `PATH` at all.
    """
    d = tmp_path / "toolbox"
    d.mkdir(exist_ok=True)
    for name in tools:
        real = sys.executable if name == "python3" else shutil.which(name)
        if real is None:
            raise ToolMissing(
                f"{name!r} is not on this host, so a stanza that shells out to it "
                "cannot be exercised here — skip on it at module scope rather than "
                "letting the test read the failure as the behaviour under test")
        link = d / name
        if not link.exists():
            link.symlink_to(real)
    return d


def sandbox_path(tmp_path: Path, *dirs: os.PathLike | str,
                 tools: Iterable[str] = ()) -> str:
    """`PATH` holding `dirs` plus a toolbox of `tools`, and provably nothing else.

    Two properties, and they are deliberately not one:

    * **every entry is inside `tmp_path`** — a directory this test made and this
      test filled. That is what makes `stub=None` mean absent: the only `qb-*`
      anywhere on the path is one the test wrote on purpose, and a stub named
      after the real tool is the point rather than a leak.
    * **the toolbox — the one directory holding things the test did not write —
      resolves no command the harness ships.**

    The guard is here rather than in a test of its own so that it runs at every
    call site. A suite that names a whole profile directory again fails on the
    call that did it.
    """
    box = toolbox(tmp_path, tools)
    root = tmp_path.resolve()
    entries = [Path(p) for p in dirs] + [box]
    outside = [str(e) for e in entries
               if e.resolve() != root and root not in e.resolve().parents]
    assert not outside, (
        f"{outside} is not under {root} — a PATH entry this test did not fill is "
        "one whose contents it cannot claim anything about, and on a host where "
        "the harness is installed that is how 'the tool is absent' came to mean "
        "'run the production tool' (#385, #472). Symlink the binaries you need "
        "with tools=(...) instead.")
    leaked = [t for t in harness_tools() if (box / t).exists()]
    assert not leaked, (
        f"the toolbox resolves {leaked} — a test that says a tool is absent would "
        "be running the real one")
    return os.pathsep.join(str(e) for e in entries)


def sibling_dir(tmp_path: Path) -> Path:
    """A directory to run a stanza's script file out of.

    The stanzas resolve `${0%/*}/qb-<tool>` when `PATH` gave them nothing, so the
    directory the script sits in is a second `PATH` of one entry and needs the
    same guarantee. Its own directory, not `tmp_path`, so that a test writing a
    fixture file next to the script cannot silently satisfy the fallback.
    """
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)
    leaked = [t for t in harness_tools() if (d / t).exists()]
    assert not leaked, f"the script's own directory holds {leaked}"
    return d
