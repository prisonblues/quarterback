"""remove-worktree must not delete the repo's default branch.

The teardown deletes the branch its worktree was on, which is right for the
`fix/issue-43` case it was written for and wrong for the one that actually
happened: a worktree created for an issue, later switched to `main`, torn down
after its PR merged. `git branch -d main` refuses (main is not merged into the
main checkout's HEAD), the script falls through to `git branch -D`, and the
repo's trunk is force-deleted as a side effect of tidying up — recoverable from
the remote, and silent, which is the bad combination.

These drive the real script against real throwaway repos rather than mocking
git, because the bug lived in the interaction between three git behaviours
(`-d` refusing, `-D` not, and `worktree list` not listing a worktree that has
just been removed) and no mock of mine would have predicted it.

Run: pytest harness/tests
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

BIN = Path(__file__).resolve().parent.parent / "bin"
REMOVE = BIN / "remove-worktree"

#: What the teardown shells out to: the names grepped out of `remove-worktree`,
#: plus the ordinary coreutils it may reach through a `$(...)` this list cannot
#: see. Generous on purpose — `toolbox()` refuses a name this host has not got,
#: so an unused entry is loud, while a MISSING one reads to the script as "that
#: tool is broken" and the test would report it as the behaviour of the guard.
TOOLS = ("git", "bash", "sh", "awk", "sed", "grep", "tr", "cat", "head", "tail",
         "wc", "date", "basename", "dirname", "rm", "mkdir", "env", "timeout",
         "jq", "chmod", "find", "sort", "mv", "ln", "readlink", "hostname")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A project with an `origin` whose HEAD is `main`, and one worktree.

    A real remote, because the guard's first move is to read `origin/HEAD` — a
    fixture with no remote would exercise only the literal-name fallback and
    pass just as happily with the guard deleted.

    **The main checkout is left on a branch that is NOT main**, which is the
    whole precondition for the bug rather than a detail of the fixture. git
    refuses two worktrees on one branch, so while the main checkout sits on
    `main` nothing else can hold it — and `remove-worktree`'s existing "branch
    still used by another worktree" test catches the case by accident. It is
    when the main checkout has wandered onto a feature branch, which is the
    normal state of an active repo, that `main` is free for a worktree to take
    and nothing at all stands between it and `git branch -D`.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)

    main_repo = tmp_path / "proj"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(main_repo)], check=True)
    git(main_repo, "config", "user.email", "t@example.com")
    git(main_repo, "config", "user.name", "t")
    (main_repo / "README.md").write_text("hi\n")
    # No .worktree.json: create-worktree needs one, teardown does not, and the
    # branch guard runs after every step that might.
    git(main_repo, "add", "-A")
    git(main_repo, "commit", "--quiet", "-m", "init")
    git(main_repo, "remote", "add", "origin", str(origin))
    git(main_repo, "push", "--quiet", "-u", "origin", "main")
    git(main_repo, "remote", "set-head", "origin", "main")
    # Off main, so main is free to be held by a worktree. See the docstring.
    assert git(main_repo, "checkout", "--quiet", "-b", "wip/current").returncode == 0
    return main_repo


def add_worktree(repo, name, branch):
    """A linked worktree at the path remove-worktree derives from `name`."""
    wt = repo.parent / f"proj-{name}"
    if branch == "main":
        # How it happens in life: created on its own branch for an issue, and
        # switched to main later. Checked rather than assumed — this same call
        # fails with "main is already used by worktree" when the main checkout
        # is on main, and a silent failure here leaves the worktree on its
        # original branch, so the teardown deletes THAT and the test passes
        # while exercising nothing.
        assert git(repo, "worktree", "add", "--quiet", "-b", f"tmp/{name}",
                   str(wt)).returncode == 0
        assert git(wt, "checkout", "--quiet", "main").returncode == 0
        assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    else:
        assert git(repo, "worktree", "add", "--quiet", "-b", branch, str(wt)).returncode == 0
    return wt


def run_remove(repo, name, tmp_path):
    """The real script, with a `PATH` and a `HOME` this test owns.

    It used to be `subprocess.run([...], cwd=repo)` with no `env=` at all, and
    that is #528: the inherited `PATH` found the installed `worktree-holder`,
    `qb-admit` and `qb-release`, the inherited `HOME` found
    `~/.config/quarterback/config`, and the three tests below made three
    authenticated `GET /active` calls to the PRODUCTION board with this machine's
    own bearer token on every local run. The guard under test is about `git
    branch -d`; the board had nothing to do with it and was reached anyway.

    Both halves are needed and neither substitutes for the other. A sandboxed
    `PATH` alone still leaves `${0%/*}/worktree-holder` resolving inside
    `harness/bin`, because the script is invoked from there by absolute path —
    so the tool is reachable no matter what `PATH` says, and the only thing that
    stops it reaching the board is having no credential to reach it with.
    """
    return subprocess.run(
        [str(REMOVE), name], cwd=repo, capture_output=True, text=True,
        env=_path_sandbox.sandbox_env(tmp_path, tools=TOOLS))


def branches(repo):
    r = git(repo, "branch", "--format=%(refname:short)")
    return set(r.stdout.split())


def test_a_worktree_parked_on_main_does_not_take_main_with_it(repo, tmp_path):
    """The bug, end to end: teardown must remove the worktree and keep main."""
    add_worktree(repo, "feat-issue-82", "main")
    assert "main" in branches(repo)

    proc = run_remove(repo, "feat-issue-82", tmp_path)

    assert "main" in branches(repo), (
        f"remove-worktree deleted the default branch.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert git(repo, "rev-parse", "--verify", "main").returncode == 0
    # And it says so, rather than leaving the operator to notice.
    assert "default branch" in proc.stdout.lower()
    # The worktree itself is still gone — the guard protects the branch only.
    assert not (repo.parent / "proj-feat-issue-82").exists()


def test_the_guard_holds_when_origin_head_is_missing(repo, tmp_path):
    """`origin/HEAD` is a local cache and a clone can simply not have it.

    Whichever way the default branch is worked out, deleting it is still wrong,
    so the fallback path is tested rather than assumed — it is the path a fresh
    `git clone --branch` actually takes.
    """
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    add_worktree(repo, "feat-issue-82", "main")

    proc = run_remove(repo, "feat-issue-82", tmp_path)

    assert "main" in branches(repo), (
        f"guard failed with no origin/HEAD.\nstdout:\n{proc.stdout}")


def test_an_ordinary_feature_branch_is_still_deleted(repo, tmp_path):
    """The guard must not turn into "never delete anything".

    Pruning the branch is the teardown's job for every branch that is not the
    trunk, and a guard that over-fires leaves a repo full of merged branches —
    quietly, which is how it would survive.
    """
    add_worktree(repo, "fix-issue-43", "fix/issue-43")
    assert "fix/issue-43" in branches(repo)

    proc = run_remove(repo, "fix-issue-43", tmp_path)

    assert "fix/issue-43" not in branches(repo), (
        f"ordinary branch survived teardown.\nstdout:\n{proc.stdout}")
    assert "deleted" in proc.stdout.lower()
