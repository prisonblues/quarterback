"""The PATH sandbox itself: absent has to mean absent (#385, #472).

`_path_sandbox` is a fixture, and a fixture that quietly stops working takes
every suite built on it with it — silently, and in the direction that reads as
success. #472 is exactly that: three suites asserted what a stanza does with
`qb-claim` / `qb-release` / `qb-admit` missing, built their PATH out of
`dirname(bash)` to get `git` and `jq`, and on a home-manager install that
directory IS the profile directory holding those three tools. The absent branch
was never taken. A fourth suite passed anyway and had never taken it either.

So the module's guarantee is asserted here, on this host, with the real tools
installed — the configuration under which the old idiom failed. Two of these
tests would have caught #472 the day it was written.

Run: pytest harness/tests
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

TESTS = Path(__file__).resolve().parent

#: The suites whose subject is a tool being missing. They are named rather than
#: discovered because the property is about intent — a suite that asserts on an
#: absent tool owes its PATH to this module — and a discovered list would go
#: quiet the moment somebody spelled the absence differently.
ABSENCE_SUITES = (
    "test_create_worktree_claim.py",
    "test_create_worktree_bound.py",
    "test_prune_worktrees_claims.py",
    "test_remove_worktree_claim.py",
)


# ----------------------------------------------------------- the guarantee

def test_no_harness_tool_resolves_on_a_sandbox_path(tmp_path):
    """The property the three failing suites believed they had.

    Asserted against every command `harness/bin` ships, not the three that
    happened to break: #385's point is that the class grows — each new `qb-*`
    with a "what if this is missing" test becomes an instance the day it reaches
    a profile directory.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    path = _path_sandbox.sandbox_path(tmp_path, bindir, tools=("git", "jq", "tr"))
    found = {t: shutil.which(t, path=path) for t in _path_sandbox.harness_tools()}
    assert not {k: v for k, v in found.items() if v is not None}


def test_the_tools_asked_for_are_there_and_nothing_else_is(tmp_path):
    """A sandbox that resolved nothing would pass the test above and fail every
    stanza that shells out — which is how this suite's own sibling first ran,
    green about a sweep that had swept nothing."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    path = _path_sandbox.sandbox_path(tmp_path, bindir, tools=("git", "tr"))
    assert shutil.which("git", path=path)
    assert shutil.which("tr", path=path)
    assert shutil.which("jq", path=path) is None, "a tool nobody asked for"
    assert shutil.which("bash", path=path) is None


def test_python3_is_the_interpreter_running_this_suite(tmp_path):
    """The rollback in `create-worktree` releases THROUGH qbdata's own client, so
    two tests there need a real interpreter — and `python3` is not necessarily on
    PATH at all, either inside a nix build sandbox or under the very run a
    developer uses to check this class of bug (`PATH` with the profile stripped).
    """
    box = _path_sandbox.toolbox(tmp_path, ("python3",))
    assert (box / "python3").resolve() == Path(sys.executable).resolve()


def test_a_directory_this_test_did_not_fill_is_refused(tmp_path):
    """#472's actual line, rejected. `dirname(bash)` reads as "somewhere the
    basics live" and is in fact "wherever this host installed bash", which on the
    fleet is the directory the harness installs into."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    with pytest.raises(AssertionError, match="is not under"):
        _path_sandbox.sandbox_path(
            tmp_path, bindir, os.path.dirname(shutil.which("bash")))


def test_a_stub_the_test_wrote_is_not_a_leak(tmp_path):
    """The other half: a suite stubbing `qb-release` in its own bin directory is
    doing the intended thing, and a guard that could not tell that apart from a
    profile directory would be unusable."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "qb-release").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "qb-release").chmod(0o755)
    path = _path_sandbox.sandbox_path(tmp_path, bindir)
    assert shutil.which("qb-release", path=path) == str(bindir / "qb-release")


def test_a_tool_this_host_has_not_got_is_an_error_not_a_silent_gap(tmp_path):
    """A missing symlink would read to the stanza as "the tool is broken", and the
    test would report that as the behaviour under test."""
    with pytest.raises(_path_sandbox.ToolMissing):
        _path_sandbox.toolbox(tmp_path, ("no-such-binary-anywhere",))


def test_the_scripts_own_directory_holds_no_harness_tool(tmp_path):
    """PATH is only half of the leak: each stanza falls back to `${0%/*}/qb-<tool>`
    when `command -v` finds nothing, so the directory the script file sits in is a
    second PATH of one entry."""
    d = _path_sandbox.sibling_dir(tmp_path)
    assert d.is_dir()
    (d / "qb-release").write_text("")
    with pytest.raises(AssertionError):
        _path_sandbox.sibling_dir(tmp_path)


def test_the_tool_list_is_read_from_harness_bin(tmp_path):
    """An empty list would make every assertion above pass while guarding
    nothing — #163's mechanism, and the reason it is asserted rather than
    assumed."""
    tools = _path_sandbox.harness_tools()
    assert {"qb-claim", "qb-release", "qb-admit"} <= set(tools)
    assert "qbdata.py" not in tools, "a module, not a command on PATH"


# ------------------------------------------------------------- the coupling

@pytest.mark.parametrize("name", ABSENCE_SUITES)
def test_the_absence_suites_build_their_path_here(name):
    """The guard that keeps this fix from being undone one file at a time.

    Each of these drives a stanza with the board tool deliberately missing. A
    hand-built PATH in any of them is the defect coming back, and it comes back
    green.
    """
    src = (TESTS / name).read_text().replace("'", '"')
    assert "_path_sandbox" in src, f"{name} builds its own PATH again"
    # Every spelling of "and then whatever the host has", not just the one #472
    # was written in. `dict(os.environ)` and `os.environ.copy()` are on the list
    # because a sweep of the rest of this tree found the same defect wearing each
    # of them — the PATH arrives by inheritance rather than by name, which reads
    # as harmless at the call site and is the identical leak.
    for banned in ("os.path.dirname(BASH)", "os.path.dirname(JQ)",
                   "os.path.dirname(TR)", 'os.environ["PATH"]',
                   "dict(os.environ)", "os.environ.copy()"):
        assert banned not in src, (
            f"{name} puts {banned} on the PATH of a test that says a tool is "
            "absent — on a host where the harness is installed that directory "
            "holds the tool (#472)")
