"""An environment a suite controls completely — its `PATH` and its `HOME`.

Two halves, landed in that order and documented in that order below:
`sandbox_path()` (#385, #472, #527) makes a tool the test did not write
ABSENT, and `sandbox_env()` (#528) makes the credential a tool it DOES run
would use absent as well. Every suite the first fixed had thought about
`PATH`; not one had thought about `HOME`.

## The `PATH` half

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

## The `HOME` half

`sandbox_env()` is at the foot of this file, with its own argument.

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


# ---------------------------------------------------------------------------
# The other half: an environment with no credential in it (#528)
#
# `sandbox_path` makes a tool ABSENT. It does nothing about a tool a test
# deliberately runs — and five suites ran real `qb-*` tools with no `env=` at
# all, so they inherited `$HOME`, therefore `~/.config/quarterback/config`,
# therefore the machine's board token. Two of them made authenticated calls to
# the production board on every local run: `test_remove_worktree_branch_guard.py`
# three `GET /active` with a live 64-character bearer, and
# `create_worktree_nginx.test.sh` one more.
#
# So the second guarantee lives here, next to the first, rather than in a module
# of its own or in each suite's fixture. The two are one property — "this test
# controls what the script it starts can reach" — and splitting them is how one
# came to be remembered and the other forgotten: every suite #527 fixed had
# thought about PATH and not one had thought about HOME.

#: Names that ARE a credential or say where one lives. Matched by prefix because
#: the set grows: `QUARTERBACK_TOKEN_REFRESH_CMD` and `QUARTERBACK_HUMAN_KEY` are
#: both in the shipped config on this fleet and neither existed when the first
#: two were written.
CREDENTIAL_PREFIXES = ("QUARTERBACK_", "QB_", "ANTHROPIC_", "CLAUDE_",
                       "GH_", "GITHUB_")

#: Names whose VALUE is a directory a credential lives under. Redirected rather
#: than deleted: a tool that finds no `$HOME` at all behaves differently from one
#: that finds an empty one, and "a machine with no quarterback config" is the
#: state being emulated — not "a machine with no home directory".
HOME_VARS = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
             "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR")


def config_path(env: dict) -> str:
    """The board config file `env` would read.

    ONE rule, spelled identically by both implementations —
    `qbdata.resolve_config()` for every python tool and `qb-env:57` for every
    bash one:

        ${QUARTERBACK_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/quarterback/config}

    Restated here so `sandbox_env` can say where an environment would look, and
    pinned to BOTH of them by live decoys in `test_path_sandbox.py` rather than by
    this docstring — a helper that invents the shape of the thing it stands for is
    how #531 shipped twelve tests that could not fail.
    """
    return env.get("QUARTERBACK_CONFIG") or os.path.join(
        env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", ""), ".config"),
        "quarterback", "config")


def sandbox_home(tmp_path: Path) -> Path:
    """An empty home directory, with the XDG tree a tool expects underneath it."""
    home = tmp_path / "home"
    for sub in (".config", ".cache", ".local/share", ".local/state"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    run = tmp_path / "xdg-run"
    run.mkdir(exist_ok=True)
    run.chmod(0o700)          # $XDG_RUNTIME_DIR is specified to be 0700
    return home


def sandbox_env(tmp_path: Path, *dirs: os.PathLike | str,
                tools: Iterable[str] = (), inherit_path: bool = False,
                **over: str) -> dict[str, str]:
    """The environment to hand a real harness script, with no credential in it.

    Derived from `os.environ` by removing what is dangerous, rather than built up
    from an allow-list of what is safe. That is the weaker of the two shapes and
    it is chosen deliberately: an allow-list would have to name every locale, CA
    bundle and nix variable that `git`, `bash` and `python3` need on the hosts
    this suite runs on, and a missing one makes a test fail for a reason that is
    not its subject — while the credential surface is small, named, and asserted
    below rather than trusted. `test_path_sandbox.py` runs the PRODUCTION readers
    against a decoy config to hold that.

    Three things happen, and the third is what makes the first two checkable:

    * every `CREDENTIAL_PREFIXES` name is dropped, so nothing is inherited;
    * `HOME_VARS` point inside `tmp_path`, and `QUARTERBACK_CONFIG` at a file
      under it that does not exist — both, not either, so a tool that honours
      only one of the two override points is covered by the other;
    * the result is asserted to name no path outside `tmp_path` for any of them.

    `QB_SEATS_PACE=off` is a default here because `warn` is the shipped default,
    it starts `qb-pace` beside whatever is under test, and `qb-pace` reads the
    DEVELOPER'S OWN subscription and calls the usage endpoint. PATH cannot make it
    absent — `qb-seats` finds `qb-pace` through `beside_me`, which falls back to
    `${0%/*}/qb-pace` and so resolves inside `harness/bin` however bare the PATH is
    — so the knob is the only seam. A test that is ABOUT the pacing sets it back
    through `over`.

    It was `QB_SEAT_PACE` (singular) until #540, which is a variable nothing reads
    any more: the estimate and the gate are one knob now and it is the plural one.
    A stale name here would be a guard that silently stopped guarding, and the
    failure — a suite quietly reading a real subscription — is invisible in a green
    run, which is why `test_path_sandbox.py` asserts the name and not just the
    behaviour.

    `inherit_path=True` keeps the ambient PATH (with `dirs` in front of it). It is
    for the suites whose subject is the real tools cooperating with a STUB board —
    `test_worktree_holder.py` starts `remove-worktree` so that it will find the
    real `worktree-holder` — where making the tools absent would delete the test.
    It is named rather than left to each call site to spell so that the coupling
    guard can see it, and it does not weaken the credential half: that is
    unconditional.
    """
    home = sandbox_home(tmp_path)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(CREDENTIAL_PREFIXES)}
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "xdg-run"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        # Deliberately absent, and named so the assertion below can say so.
        "QUARTERBACK_CONFIG": str(home / "no-such-quarterback-config"),
        "QB_SEATS_PACE": "off",
    })
    if inherit_path:
        # Empty entries dropped rather than joined through: an empty PATH element
        # means the CURRENT DIRECTORY, so `join([dir, ""])` on a host with no
        # PATH at all would put the tree under test on its own PATH.
        env["PATH"] = os.pathsep.join(
            [p for p in (*(str(d) for d in dirs),
                         *os.environ.get("PATH", "").split(os.pathsep)) if p])
    else:
        env["PATH"] = sandbox_path(tmp_path, *dirs, tools=tools)
    env.update({k: str(v) for k, v in over.items()})

    root = tmp_path.resolve()

    def inside(value: str) -> bool:
        """Is `value` somewhere this test made?

        A value that is not ABSOLUTE passes, and that is not a hole. An empty or
        relative `$HOME` resolves against the CHILD's working directory, which is
        a directory the caller chose — it cannot reach `~/.config/quarterback/
        config` however it is spelled. `test_qb_start.py` passes `HOME=""` on
        purpose: the gate it is about is reached by the variable being absent,
        and refusing that here would delete the test rather than isolate it.
        """
        if not value or not os.path.isabs(value):
            return True
        resolved = Path(value).resolve()
        return resolved == root or root in resolved.parents

    outside = {k: env[k] for k in (*HOME_VARS, "QUARTERBACK_CONFIG")
               if k in env and not inside(env[k])}
    assert not outside, (
        f"{outside} points outside {root} — a script started with this "
        "environment reads the developer's own config, which is how a harness "
        "run came to make authenticated calls to the production board (#528).")
    assert inside(config_path(env)), (
        f"the board config this environment resolves is {config_path(env)}, "
        f"which is not under {root}. A file the TEST wrote there is the intended "
        "thing — the three suites that pin the resolution rule supply their own "
        "decoy — but a path outside it is the developer's own, and reading it "
        "is #528.")
    #: The names this function set itself, which are the only credential-shaped
    #: ones allowed to survive — plus whatever the caller asked for by name,
    #: which is a decision it made rather than one it inherited.
    ours = {*HOME_VARS, "QUARTERBACK_CONFIG", "QB_SEATS_PACE", *over}
    leaked = sorted(k for k in env
                    if k.startswith(CREDENTIAL_PREFIXES) and k not in ours)
    assert not leaked, f"{leaked} was inherited from the developer's shell"
    return env
