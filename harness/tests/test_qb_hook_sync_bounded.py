"""`qb-hook`'s `_sync_note` must not hang on a git that does (the /mnt/c report).

On a WSL checkout under /mnt/c, `git status` sits in D state on the 9p bridge for a
minute or more. `_sync_note` runs on every prompt, so an unbounded call there
stalls the whole turn. These drive the real script with a `git` that hangs on
`status` and assert the hook still answers, and says "unknown" rather than "clean".

Run: pytest harness/tests
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_qb_hook_end import Hooked  # noqa: E402

REAL_GIT = shutil.which("git")
SLEEP = shutil.which("sleep")  # absolute: the sandbox PATH has no sleep

pytestmark = pytest.mark.skipif(
    REAL_GIT is None or shutil.which("jq") is None, reason="needs git and jq")


def _repo(path: Path) -> None:
    path.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
        "--allow-empty", "-m", "x")


@pytest.fixture
def slow_status(tmp_path):
    hook = Hooked(tmp_path)
    hook.cwd = tmp_path / "repo"
    _repo(hook.cwd)
    # Hang on `status` only; every other subcommand is the real git.
    (hook.stub / "git").write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do [ "$a" = status ] && exec {SLEEP} 60; done\n'
        f'exec {REAL_GIT} "$@"\n'
    )
    (hook.stub / "git").chmod(0o755)
    return hook


def test_a_hanging_git_status_does_not_stall_the_sync_note(slow_status):
    start = time.monotonic()
    slow_status.fire("UserPromptSubmit", prompt="hi")
    assert time.monotonic() - start < 20


def test_dirty_is_left_out_when_status_times_out(slow_status):
    slow_status.fire("UserPromptSubmit", prompt="hi")
    sync = slow_status.to("/sync")
    # Unknown is not clean: omit the parameter rather than send dirty=false.
    assert sync, slow_status.sent()
    assert all("dirty=" not in c for c in sync), sync
