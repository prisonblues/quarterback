"""nginx-block regressions in create-worktree / remove-worktree.

The assertions live in `create_worktree_nginx.test.sh` next door; this file
exists so CI collects them. The suite drives the real scripts against throwaway
git repos with a stubbed `docker`, so the nginx step runs for real and its output
is read back off the generated config — which is why it is bash: it is a test
about shell scripts, using shell stubs, and it was already written.

It came from nix-fleet, where these scripts used to live. They moved here in
nix-fleet's 80e8f18 and the test stayed behind pointing at a path that no longer
existed, so it hard-failed on its first line and tested nothing. nix-fleet has no
CI, so nothing said so. Wrapping rather than hand-porting is deliberate: the
cases below encode regressions somebody already paid for (lexray #1501 among
them), and a 220-line port is an opportunity to drop one quietly — which is
precisely the failure this suite is about.

One test per bash case, not one test for the whole suite (#785). The wrapper used
to shell out to the entire script as a single test, and a single test is a single
xdist worker: with `-n 32` no suite can finish sooner than its longest test, so
this file alone held the harness suite's floor at ~24s while the other ~3250
tests finished around it. Each scenario is now its own node id, and the floor is
the slowest scenario rather than their sum.

The case names are read out of the bash file rather than listed here. A
hardcoded mirror of a list is the failure this repo keeps re-learning: it goes on
passing while quietly not covering whatever was added to the list last, which is
the same silence the suite itself was resurrected from. `test_case_list_matches_suite`
guards the discovery, so a rename that makes it match nothing fails loudly
instead of collecting zero tests.

Run: pytest harness/tests
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parent / "create_worktree_nginx.test.sh"
BIN = Path(__file__).resolve().parent.parent / "bin"

# A bash function definition at column zero whose name starts `case_`. The stub
# heredocs inside the suite contain a `case "${1:-}" in` of their own, which the
# underscore keeps out of this.
_CASE_DEF = re.compile(r"^case_([a-z0-9_]+)\(\)\s*\{", re.MULTILINE)


def _discover_cases() -> list[str]:
    """Every `case_<name>()` the bash suite defines, in source order.

    Parsed from the text rather than taken from the suite's own `--list`, because
    this runs at collection time and a subprocess per collection is both slow and
    a way for the whole file to error out instead of failing one test. The two
    routes are cross-checked in `test_case_list_matches_suite`, which is what
    makes the cheap one trustworthy: parsing finds functions, `--list` prints the
    CASES array the suite dispatches from, and a case defined but never
    registered (or registered but never defined) shows up as a mismatch.
    """
    if not SUITE.is_file():
        return []
    return _CASE_DEF.findall(SUITE.read_text())


CASES = _discover_cases()

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required to build the fixtures"
)


def _run_suite(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SUITE), *args],
        capture_output=True,
        text=True,
        timeout=600,
    )


@needs_git
@pytest.mark.parametrize("case", CASES)
def test_nginx_block_regressions(case):
    """Run one bash case; on failure surface its whole report, not just a code."""
    assert SUITE.is_file(), f"suite missing: {SUITE}"
    for script in ("create-worktree", "remove-worktree"):
        assert (BIN / script).is_file(), f"script under test missing: {BIN / script}"

    proc = _run_suite(case)
    if proc.returncode != 0:
        pytest.fail(
            f"create_worktree_nginx.test.sh {case} failed (rc={proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )


@needs_git
def test_case_list_matches_suite():
    """The discovered cases are non-empty and are exactly what the suite runs.

    Without this, a rename of the `case_` prefix — or a case function that never
    made it into the suite's CASES array — leaves the parametrisation matching
    nothing, and pytest reports no failures because it collected no tests. That
    is indistinguishable from green on the summary line, and this file has been
    that kind of green before.

    Compared as sets: CASES is the run order of the standalone script and is free
    to differ from the order the functions happen to be written in.
    """
    assert CASES, f"no case_<name>() functions found in {SUITE}"
    assert len(CASES) == len(set(CASES)), f"duplicate case names: {CASES}"

    proc = _run_suite("--list")
    assert proc.returncode == 0, f"--list failed (rc={proc.returncode}): {proc.stderr}"
    listed = proc.stdout.split()

    assert sorted(listed) == sorted(CASES), (
        "the cases the suite dispatches and the cases it defines have diverged\n"
        f"  defined but not in CASES: {sorted(set(CASES) - set(listed))}\n"
        f"  in CASES but not defined: {sorted(set(listed) - set(CASES))}"
    )


@needs_git
def test_unknown_case_is_not_silently_green():
    """A name the suite does not know must fail, not report an empty pass.

    The wrapper hands the suite a name it read out of the file, so this can only
    break in one direction — the two drifting apart — and the drift is silent
    unless a missed case exits non-zero. Asserted here rather than assumed
    because the whole point of discovery is that nobody is checking by hand.
    """
    proc = _run_suite("no_such_case_at_all")
    assert proc.returncode != 0, f"unknown case exited 0:\n{proc.stdout}"
    assert "no such case" in proc.stderr
