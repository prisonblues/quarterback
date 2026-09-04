"""`qb-dash` the LAUNCHER — argument handling and the interpreter search.

`test_qb_dash.py` loads `qb-dash-tui.py` and tests the dashboard. Nothing tested
the shell script in front of it, which is a gap that mattered the moment the plain
renderer was retired: everything the script does now is compatibility, and
compatibility is exactly the kind of code that breaks silently.

WHAT THESE PROVE IS ARGV ROUTING AND THE INTERPRETER SEARCH, and not that a
dashboard comes up. The stub interpreter prints its arguments and the target file
is empty, so a test here would pass against a `qb-dash-tui.py` that could not run
at all — that end is `test_qb_dash.py`'s. Read the names with that bound on them.

`--tui` USED TO SELECT A RENDERER and now selects nothing, because there is only
one. It is still accepted, and that is not politeness: an installed `qb-seats`
older than this change still emits `qb-dash --tui`, and `qb-dash-tui` execs it too.
A launcher that rejected the flag naming the thing it always does would turn a
partial rebuild into a dash pane full of argparse.

`--can-tui` is the other half of the same window: that older `qb-seats` asks it
before deciding, and a nonzero answer sends it to a renderer that is no longer
installed. It has to keep saying yes wherever the dashboard can actually run.

Run: pytest harness/tests/test_qb_dash_launcher.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

#: Prints one argument per line, so a test can assert on argv exactly rather than
#: on a substring of a joined string — `--tui` is a substring of `--tui-ish` and of
#: any path containing it, and this file's whole subject is which flags survive.
STUB_PY = """#!/bin/sh
if [ "$1" = "-c" ]; then exit %d; fi
for a in "$@"; do printf '%%s\\n' "$a"; done
"""


@pytest.fixture
def pkg(tmp_path):
    """A bin/ that looks like an installed harness, away from the real one.

    `qb-dash` finds itself by looking for `qbdata.py` beside it, and searches
    `<bin>/../../mcp/.venv/bin/python` among its candidates. Copying it somewhere
    empty is what stops that candidate resolving to this checkout's own venv, which
    has textual and would answer yes in the test that needs a no.
    """
    d = tmp_path / "pkg" / "bin"
    d.mkdir(parents=True)
    (d / "qb-dash").write_bytes((BIN / "qb-dash").read_bytes())
    (d / "qb-dash").chmod(0o755)
    (d / "qbdata.py").write_text("")
    (d / "qb-dash-tui.py").write_text("")
    return d


def run(pkg, *args, importable=True, **env):
    """`qb-dash` with a stub interpreter, and nothing of the host to fall back on.

    HOME and `python3` are both pinned because both are candidates the launcher
    searches — `$HOME/source/quarterback/mcp/.venv/bin/python` and whatever
    `python3` is on PATH — and a test that let either through would pass on this
    machine for a reason it could not state.

    `python3` is SHADOWED rather than the PATH being emptied. An empty one takes
    `bash` with it and the script never starts: `#!/usr/bin/env bash` is a PATH
    lookup too, and so are the `dirname` and `readlink` in `self_dir`. So the real
    PATH stays, with a directory in front of it holding the same stub under the
    name the search will look for.
    """
    stub = pkg.parent / "python"
    stub.write_text(STUB_PY % (0 if importable else 1))
    stub.chmod(0o755)
    shadow = pkg.parent / "shadow"
    shadow.mkdir(exist_ok=True)
    (shadow / "python3").write_text(STUB_PY % (0 if importable else 1))
    (shadow / "python3").chmod(0o755)
    base = {"HOME": str(pkg.parent),
            "PATH": f"{shadow}:{os.environ['PATH']}",
            "QB_DASH_PYTHON": str(stub)}
    base.update(env)
    # A value of None means "leave this one out of the environment entirely",
    # which is the only way to ask for an UNSET variable rather than an empty one
    # — and empty is not the case that broke.
    return subprocess.run(
        [str(pkg / "qb-dash"), *args], capture_output=True, text=True,
        env={k: v for k, v in base.items() if v is not None})


def argv(done) -> list[str]:
    return done.stdout.splitlines()


def test_the_dashboard_is_launched_with_no_renderer_flag(pkg):
    """The plain path: `qb-dash` runs the one dashboard there is."""
    done = run(pkg, "--scope", "all")
    assert done.returncode == 0, done.stderr
    assert argv(done) == [str(pkg / "qb-dash-tui.py"), "--scope", "all"]


def test_tui_is_accepted_and_never_reaches_the_dashboard(pkg):
    """Accepted for the unrebuilt `qb-seats` that still emits it; dropped because
    the Python has never known the flag and argparse would reject it."""
    done = run(pkg, "--tui", "--scope", "all")
    assert done.returncode == 0, done.stderr
    assert "--tui" not in argv(done)
    assert argv(done) == [str(pkg / "qb-dash-tui.py"), "--scope", "all"]


def test_tui_is_recognised_anywhere_not_only_first(pkg):
    """`qb-dash --scope all --tui` is a thing a person types. It used to be matched
    with `case "${1:-}"`, which ran the PLAIN renderer and then died on argparse's
    "unrecognized arguments: --tui" — the bug that made this a loop."""
    done = run(pkg, "--scope", "all", "--tui")
    assert argv(done) == [str(pkg / "qb-dash-tui.py"), "--scope", "all"]


def test_everything_after_a_double_dash_belongs_to_the_target(pkg):
    """Including a literal `--tui`, which past the separator is an argument and not
    this script's business."""
    done = run(pkg, "--tui", "--", "--tui", "-x")
    assert argv(done) == [str(pkg / "qb-dash-tui.py"), "--", "--tui", "-x"]


def test_can_tui_says_yes_where_the_dashboard_can_run(pkg):
    """The compatibility answer. An older `qb-seats` asks this before deciding
    which renderer to put in the pane, and a no sends it to one that is no longer
    installed — so on a box where the dashboard runs, this must say so."""
    assert run(pkg, "--can-tui").returncode == 0


def test_can_tui_says_no_when_nothing_can_import_the_dashboard(pkg):
    assert run(pkg, "--can-tui", importable=False).returncode == 1


def test_an_unimportable_dashboard_fails_loudly_rather_than_falling_back(pkg):
    """There is no lesser renderer left to drop to, so the error has to carry the
    remedy. The plain one used to be the answer here."""
    done = run(pkg, importable=False)
    assert done.returncode == 1
    assert "QB_DASH_PYTHON" in done.stderr
    assert "textual" in done.stderr
    assert not argv(done), "the dashboard was launched on an interpreter that fails"


def test_a_missing_HOME_does_not_take_the_search_down_with_it(pkg):
    """`$HOME` names one candidate among several, so an unset one must cost that
    candidate and nothing else.

    It used to cost the whole run. The candidate list was a fixed array holding a
    bare `$HOME`, and under `set -u` building it aborted the script — `HOME:
    unbound variable`, a line number where the dependency error should have been,
    and `python3` never tried although it might have worked. `env -i`, a cron job
    and a systemd unit all define no HOME.

    `qb-board` builds the same list by appending only the candidates that exist
    and says in a comment that this is partly to keep `set -u` happy about $HOME.
    This is that fix, arriving in the other launcher.
    """
    done = run(pkg, "--scope", "all", QB_DASH_PYTHON=None, HOME=None)
    assert done.returncode == 0, done.stderr
    assert "unbound variable" not in done.stderr
    # Fell through to the shadowed `python3`, which is the candidate that was
    # being skipped: the launch happened, on the interpreter furthest down.
    assert argv(done) == [str(pkg / "qb-dash-tui.py"), "--scope", "all"]


def test_a_missing_HOME_still_reaches_the_dependency_error(pkg):
    """The other half: when nothing can import it, the reason has to be the reason
    — not the shell's complaint about an unset variable."""
    done = run(pkg, importable=False, QB_DASH_PYTHON=None, HOME=None)
    assert done.returncode == 1
    assert "unbound variable" not in done.stderr
    assert "QB_DASH_PYTHON" in done.stderr
