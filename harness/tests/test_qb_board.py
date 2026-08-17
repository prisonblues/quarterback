"""Tests for qb-board, the launcher for the terminal board client.

The launcher is bash and the client is Python, and everything that can go wrong
here goes wrong *before* a line of Python runs: it walks a symlink chain to find
its own directory, tries four candidate interpreters in order, and has to say
something actionable when none of them can import the client. A launcher that
picks the wrong interpreter fails as an ImportError from somewhere unexpected,
and one that exits silently is worse — the whole reason the not-found message is
in the file is that "nothing happened" is not a usable answer on a headless box.

Every interpreter here is a stub shell script rather than a real Python. The
launcher's contract with it is exactly two invocations — `-c 'import
mcp_server.board'` to probe, and `-m mcp_server.board "$@"` to hand over — so a
stub can answer both, and a stub can be made *unusable* on demand, which a real
interpreter cannot.

Run: pytest harness/tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

QB_BOARD = Path(__file__).resolve().parent.parent / "bin" / "qb-board"

#: What the stub prints when the launcher hands over. `$0` is the path the
#: launcher invoked it by, which is the assertion that matters: *which*
#: candidate won, not merely that one did.
_STUB = """#!/bin/sh
if [ "$1" = "-c" ]; then exit {probe} ; fi
if [ "$1" = "-m" ]; then
  shift 2
  printf 'LAUNCH:%s\\n' "$0"
  for a in "$@"; do printf 'ARG:%s\\n' "$a"; done
  exit {status}
fi
exit 99
"""


def _stub_python(path: Path, *, usable: bool = True, status: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_STUB.format(probe=0 if usable else 1, status=status))
    path.chmod(0o755)
    return path


@pytest.fixture
def launcher(tmp_path):
    """Run qb-board from a copy, with every candidate source under our control.

    A copy rather than the real `harness/bin/qb-board`, because the third
    candidate is `$BIN_DIR/../../mcp/.venv/bin/python` — from the real path that
    is this checkout's own venv, which exists and works, so every test would
    resolve to it and none of the ordering would be exercised.
    """
    repo = tmp_path / "repo"
    bin_dir = repo / "harness" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy2(QB_BOARD, bin_dir / "qb-board")
    (bin_dir / "qb-board").chmod(0o755)

    # A python3 on PATH that cannot import the client: the launcher's last
    # resort must be *tried* in every test and must succeed in none of them by
    # accident, and the host's real python3 would decide that differently on
    # different machines.
    path_dir = tmp_path / "path-bin"
    _stub_python(path_dir / "python3", usable=False)

    home = tmp_path / "home"
    home.mkdir()

    def run(*args, env=None, script=None):
        base = {
            "PATH": f"{path_dir}:{os.environ['PATH']}",
            "HOME": str(home),
        }
        base.update(env or {})
        return subprocess.run(
            [str(script or bin_dir / "qb-board"), *args],
            env=base,
            capture_output=True,
            text=True,
        )

    run.repo = repo
    run.bin_dir = bin_dir
    run.path_dir = path_dir
    run.home = home
    run.tmp = tmp_path
    return run


def _launched(result) -> str:
    for line in result.stdout.splitlines():
        if line.startswith("LAUNCH:"):
            return line[len("LAUNCH:") :]
    raise AssertionError(f"nothing was launched: {result.stdout!r} / {result.stderr!r}")


def _args(result) -> list[str]:
    return [ln[len("ARG:") :] for ln in result.stdout.splitlines() if ln.startswith("ARG:")]


# -- handing over ------------------------------------------------------


def test_arguments_reach_the_client_unchanged(launcher):
    py = _stub_python(launcher.tmp / "explicit" / "python")
    result = launcher("--follow", "-n", "5", env={"QB_BOARD_PYTHON": str(py)})
    assert result.returncode == 0, result.stderr
    assert _launched(result) == str(py)
    assert _args(result) == ["--follow", "-n", "5"]


def test_a_leading_board_verb_is_forwarded_rather_than_stripped_here(launcher):
    """`board) exec qb-board "$@"` in `qb` leaves the verb on; the Python drops it.

    If the launcher stripped it too, the `_strip_verb` that exists for exactly
    this would never see the argument it was written for — and the day `qb`'s arm
    is written with a `shift` instead, the first real argument would vanish.
    """
    py = _stub_python(launcher.tmp / "explicit" / "python")
    result = launcher("board", "--follow", env={"QB_BOARD_PYTHON": str(py)})
    assert _args(result) == ["board", "--follow"]


def test_the_clients_exit_status_is_the_launchers_exit_status(launcher):
    """It `exec`s, so a tail that ended on a rejected token still reports 1."""
    py = _stub_python(launcher.tmp / "explicit" / "python", status=3)
    assert launcher(env={"QB_BOARD_PYTHON": str(py)}).returncode == 3


# -- candidate resolution ----------------------------------------------


def test_qb_board_python_wins_over_every_other_candidate(launcher):
    explicit = _stub_python(launcher.tmp / "explicit" / "python")
    _stub_python(launcher.tmp / "checkout" / "mcp" / ".venv" / "bin" / "python")
    _stub_python(launcher.home / "source" / "quarterback" / "mcp" / ".venv" / "bin" / "python")
    result = launcher(
        env={
            "QB_BOARD_PYTHON": str(explicit),
            "QUARTERBACK_REPO": str(launcher.tmp / "checkout"),
        }
    )
    assert _launched(result) == str(explicit)


def test_quarterback_repo_wins_over_the_home_relative_checkout(launcher):
    repo_py = _stub_python(launcher.tmp / "checkout" / "mcp" / ".venv" / "bin" / "python")
    _stub_python(launcher.home / "source" / "quarterback" / "mcp" / ".venv" / "bin" / "python")
    result = launcher(env={"QUARTERBACK_REPO": str(launcher.tmp / "checkout")})
    assert _launched(result) == str(repo_py)


def test_the_sibling_checkout_is_found_through_a_symlinked_launcher(launcher):
    """home-manager installs each file as its own flat store path and symlinks it.

    `readlink -f` would resolve straight past the bin/ directory into the store,
    so the script walks the chain one link at a time — and this is the test that
    the walk lands on the directory the launcher was installed *from*.
    """
    sibling = _stub_python(launcher.repo / "mcp" / ".venv" / "bin" / "python")
    profile = launcher.tmp / "profile" / "bin"
    profile.mkdir(parents=True)
    link = profile / "qb-board"
    link.symlink_to(launcher.bin_dir / "qb-board")
    result = launcher(script=link)
    # Resolved before comparing: the candidate is built relative to bin/ and so
    # arrives as `…/harness/bin/../../mcp/…`, which is the same interpreter.
    assert Path(_launched(result)).resolve() == sibling.resolve()


def test_the_home_relative_checkout_is_tried_before_path(launcher):
    home_py = _stub_python(
        launcher.home / "source" / "quarterback" / "mcp" / ".venv" / "bin" / "python"
    )
    _stub_python(launcher.path_dir / "python3", usable=True)
    assert _launched(launcher()) == str(home_py)


def test_a_python3_on_path_is_the_last_resort(launcher):
    """The right answer for anyone who pip-installed the package, checkout or no."""
    _stub_python(launcher.path_dir / "python3", usable=True)
    assert _launched(launcher()) == str(launcher.path_dir / "python3")


def test_an_interpreter_that_cannot_import_the_client_is_skipped(launcher):
    """The probe is the point: an interpreter that exists is not an interpreter that works."""
    broken = _stub_python(launcher.tmp / "broken" / "python", usable=False)
    home_py = _stub_python(
        launcher.home / "source" / "quarterback" / "mcp" / ".venv" / "bin" / "python"
    )
    assert _launched(launcher(env={"QB_BOARD_PYTHON": str(broken)})) == str(home_py)


# -- the environments that used to break it ----------------------------


def test_an_empty_quarterback_repo_does_not_become_a_candidate(launcher):
    """It expanded to the literal `/mcp/.venv/bin/python`, skipped by a matching string.

    That string is invisible coupling: edit the candidate expression and the
    sentinel keeps matching nothing, silently probing `/mcp` on every launch. The
    candidate is now only appended when the variable has a value, so the
    assertion is that an empty one leaves no trace at all.
    """
    home_py = _stub_python(
        launcher.home / "source" / "quarterback" / "mcp" / ".venv" / "bin" / "python"
    )
    result = launcher(env={"QUARTERBACK_REPO": ""})
    assert _launched(result) == str(home_py)
    assert result.stderr == ""


def test_an_environment_with_no_home_still_reports_how_to_configure_one(launcher):
    """Minimal cron and systemd environments have no HOME, and `set -u` is on.

    Without a guard the launcher died on the unbound variable before it could
    print the one thing it exists to print.
    """
    # Built by hand rather than through the fixture: HOME has to be *absent*
    # from the child's environment, which no override value can express.
    result = subprocess.run(
        [str(launcher.bin_dir / "qb-board")],
        env={"PATH": f"{launcher.path_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "could not find a Python" in result.stderr
    assert "unbound variable" not in result.stderr


def test_with_no_usable_interpreter_it_says_which_two_knobs_fix_it(launcher):
    result = launcher()
    assert result.returncode == 1
    assert "could not find a Python that can import mcp_server.board" in result.stderr
    assert "QUARTERBACK_REPO" in result.stderr
    assert "QB_BOARD_PYTHON" in result.stderr
    assert result.stdout == ""
