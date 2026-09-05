"""`remove-worktree --require-lock`: a timer must not read silence as consent (#743).

`worktree-holder` has three answers and the teardown used to act on one of them.
Exit 3 ("held by a named live agent") stopped it; exit 4 ("could not tell — no
board configured or reachable, no curl/jq") and a missing helper both fell
through to `return 0`, so an unreachable board read as permission to delete a
worktree, its docker stack, its database and its local branch.

That was a deliberate choice and it is the right one for a person at a terminal:
somebody typed the command, knows which tree it names, and is watching. It
stopped being the right one on 2026-09-04, when zeus began running
`stack-reaper` hourly and unattended. Nobody is watching the reaper.

So the fail-open default stays, and the strict contract is opt-in:
`--require-lock`, or `QB_UNATTENDED=1` for a caller that is a systemd unit
rather than a command line. It changes ONE answer — "could not tell" — and
these tests pin both halves, because a strict mode that also refuses when the
board says "nobody is here" is a mode that turns the reaper off.

Run: pytest harness/tests
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

BIN = Path(__file__).resolve().parent.parent / "bin"
REMOVE = BIN / "remove-worktree"

TOOLS = ("git", "bash", "sh", "awk", "sed", "grep", "tr", "cat", "head", "tail",
         "wc", "date", "basename", "dirname", "rm", "mkdir", "env", "timeout",
         "jq", "chmod", "find", "sort", "mv", "ln", "readlink", "tar", "mktemp",
         "curl", "gzip")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False)


@pytest.fixture
def repo(tmp_path):
    main = tmp_path / "proj"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(main)], check=True)
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / "README.md").write_text("hi\n")
    git(main, "add", "-A")
    git(main, "commit", "--quiet", "-m", "init")
    assert git(main, "checkout", "--quiet", "-b", "wip/current").returncode == 0
    return main


@pytest.fixture
def worktree(repo):
    wt = repo.parent / "proj-fix-issue-43"
    assert git(repo, "worktree", "add", "--quiet", "-b", "fix/issue-43",
               str(wt)).returncode == 0
    return wt


def holder_stub(tmp_path, code):
    """A `worktree-holder` on PATH that always answers `code`.

    A stub rather than the real tool against a stub board, because the subject
    here is what `remove-worktree` DOES with each answer — the tool's own
    behaviour is `test_worktree_holder.py`'s. `command -v` finds this before the
    `${0%/*}` sibling fallback ever runs, so the real one is not consulted.
    """
    d = tmp_path / "holder-stub"
    d.mkdir(exist_ok=True)
    (d / "worktree-holder").write_text(f"#!/bin/sh\nexit {code}\n")
    (d / "worktree-holder").chmod(0o755)
    return d


def run_remove(repo, tmp_path, *args, path_extra=(), script=REMOVE, **over):
    env = _path_sandbox.sandbox_env(tmp_path, *path_extra, tools=TOOLS, **over)
    return subprocess.run([str(script), *args], cwd=repo, capture_output=True,
                          text=True, env=env, check=False)


# --------------------------------------------------------------------------
# The default is unchanged: a board that cannot be reached never blocks anyone.

def test_the_interactive_default_still_proceeds_when_the_board_is_down(
        repo, worktree, tmp_path):
    """The header's reasoning, kept: a coordination tool that cannot be reached
    must not make a worktree unusable."""
    proc = run_remove(repo, tmp_path, "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 4),))

    assert not worktree.exists(), (
        f"the fail-open default was lost:\n{proc.stdout}\n{proc.stderr}")


def test_the_interactive_default_still_proceeds_with_no_helper_installed(
        repo, worktree, tmp_path):
    """The harness has to work on a host that never installed the helper."""
    lone = tmp_path / "lonely-bin"
    lone.mkdir()
    shutil.copy(REMOVE, lone / "remove-worktree")
    # The copy defeats `${0%/*}/worktree-holder`; the sandbox PATH defeats
    # `command -v`. Both, because either alone leaves the real tool reachable
    # on a host where the harness is installed (#385, #472, #528).
    proc = run_remove(repo, tmp_path, "fix-issue-43",
                      script=lone / "remove-worktree")

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"


# --------------------------------------------------------------------------
# Strict mode turns "could not tell" into a refusal, and nothing else.

def test_require_lock_refuses_when_the_holder_check_cannot_tell(
        repo, worktree, tmp_path):
    """Exit 4 is the answer that used to read as permission to delete."""
    proc = run_remove(repo, tmp_path, "--require-lock", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 4),))

    assert proc.returncode != 0
    assert worktree.is_dir(), (
        f"--require-lock deleted a worktree it could not vouch for:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "--require-lock" in proc.stderr
    # Refused BEFORE anything is destroyed — the branch is still here too.
    assert git(repo, "rev-parse", "--verify", "fix/issue-43").returncode == 0


def test_qb_unattended_is_the_environment_spelling_of_the_same_thing(
        repo, worktree, tmp_path):
    """A systemd unit sets an environment, not an argv."""
    proc = run_remove(repo, tmp_path, "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 4),),
                      QB_UNATTENDED="1")

    assert proc.returncode != 0
    assert worktree.is_dir(), f"{proc.stdout}\n{proc.stderr}"


def test_require_lock_refuses_when_the_helper_is_not_installed(
        repo, worktree, tmp_path):
    """"No helper here" is not evidence that the tree is free.

    The fail-open default proceeds on this, deliberately. An unattended caller
    has nobody to notice that its safety check has been absent for a week.
    """
    lone = tmp_path / "lonely-bin"
    lone.mkdir()
    shutil.copy(REMOVE, lone / "remove-worktree")

    proc = run_remove(repo, tmp_path, "--require-lock", "fix-issue-43",
                      script=lone / "remove-worktree")

    assert proc.returncode != 0
    assert worktree.is_dir(), f"{proc.stdout}\n{proc.stderr}"


def test_require_lock_still_tears_down_a_worktree_the_board_says_is_free(
        repo, worktree, tmp_path):
    """Strict must not collapse into "never delete anything".

    The reaper exists because the manual sweep ran roughly once per 34 worktrees
    created; a strict mode that refuses the ordinary case is the reaper switched
    off, and it would be switched off quietly.
    """
    proc = run_remove(repo, tmp_path, "--require-lock", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 0),))

    assert not worktree.exists(), (
        f"--require-lock refused a worktree the board said was free:\n"
        f"{proc.stdout}\n{proc.stderr}")


def test_require_lock_reports_a_real_holder_the_way_it_always_did(
        repo, worktree, tmp_path):
    """Exit 3 already refused, and its message is the one people act on."""
    proc = run_remove(repo, tmp_path, "--require-lock", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 3),))

    assert proc.returncode != 0
    assert worktree.is_dir()
    assert "Another live agent" in proc.stderr
    assert "--force" in proc.stderr


def test_force_still_beats_the_strict_check(repo, worktree, tmp_path):
    """--force is the documented "I have looked", and it stays available.

    Without it a box with a permanently unreachable board could never reap
    anything, which is the state that makes people delete the guard instead.
    """
    proc = run_remove(repo, tmp_path, "--require-lock", "--force", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 4),))

    assert not worktree.exists(), f"{proc.stdout}\n{proc.stderr}"


def test_an_unknown_flag_is_still_an_error(repo, worktree, tmp_path):
    """A typo'd `--require-locks` must not mean "the permissive default".

    The strict mode is the argument an unattended caller relies on, and a
    silently ignored one is the same accident spelled differently.
    """
    proc = run_remove(repo, tmp_path, "--require-locks", "fix-issue-43",
                      path_extra=(holder_stub(tmp_path, 4),))

    assert proc.returncode != 0
    assert worktree.is_dir()
    assert "Unknown option" in proc.stderr
