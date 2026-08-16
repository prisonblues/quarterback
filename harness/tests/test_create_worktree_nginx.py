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

Run: pytest harness/tests
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parent / "create_worktree_nginx.test.sh"
BIN = Path(__file__).resolve().parent.parent / "bin"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required to build the fixtures")
def test_nginx_block_regressions():
    """Run the bash suite; on failure surface its whole report, not just a code."""
    assert SUITE.is_file(), f"suite missing: {SUITE}"
    for script in ("create-worktree", "remove-worktree"):
        assert (BIN / script).is_file(), f"script under test missing: {BIN / script}"

    proc = subprocess.run(
        ["bash", str(SUITE)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"create_worktree_nginx.test.sh failed (rc={proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
