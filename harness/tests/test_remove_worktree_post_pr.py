"""The guard that stops `remove-worktree` deleting a branch whose PR never took
all of its commits.

Deleting the branch is the step that loses work, and the commits most at risk are
the ones added AFTER the PR: the post-merge tweak nobody pushed. Three were found
sitting in reapable worktrees on this machine.

The waterline is the PR's own head SHA, not the default branch. Comparing against
the default branch is the obvious check and it is wrong wherever PRs do not target
it — lexray merges into `fca` and `test` while `main` sits frozen, so
`git branch --merged main` calls every branch unmerged.

The guard is advisory by construction: no gh, no PR, or an object we do not hold
locally all mean "cannot tell", and cannot-tell must never block a teardown.

Run: pytest harness/tests/test_remove_worktree_post_pr.py
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
REMOVE_WORKTREE = BIN / "remove-worktree"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_START = "# >>> post-pr-guard"
_END = "# <<< post-pr-guard"


def _guard_block() -> str:
    text = REMOVE_WORKTREE.read_text()
    return text[text.index(_START): text.index(_END)]


def _fake_gh(binder: Path, head: str | None) -> Path:
    """A `gh` that answers the one query the guard makes. `None` stands for the
    no-PR case, where `--json headRefOid -q '.[0].headRefOid'` prints nothing."""
    binder.mkdir(parents=True, exist_ok=True)
    gh = binder / "gh"
    # POSIX /bin/sh, not /usr/bin/env: there is no /usr/bin/env inside a nix build
    # sandbox, and a stub that cannot exec fails the suite for the wrong reason.
    gh.write_text("#!/bin/sh\n" + (f'echo "{head}"\n' if head else "true\n"))
    gh.chmod(0o755)
    return binder


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "GIT_CONFIG_GLOBAL": str(tmp_path / "gc"), "GIT_CONFIG_SYSTEM": str(tmp_path / "gs")}
    (tmp_path / "gc").write_text("")
    (tmp_path / "gs").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True, env=env)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
        subprocess.run(["git", "-C", str(r), "config", k, v], check=True, env=env)
    (r / "f.txt").write_text("v1\n")
    subprocess.run(["git", "-C", str(r), "add", "f.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "checkout", "-qb", "feat/x"], check=True, env=env)
    (r / "f.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(r), "commit", "-aqm", "in the PR"], check=True, env=env)
    at_pr = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True, env=env).stdout.strip()
    return r, at_pr, env


def _run(repo_dir: Path, env: dict, *, head, force="false", keep_branch="false",
         branch="feat/x", extra_path: Path | None = None):
    e = dict(env)
    binder = _fake_gh(repo_dir.parent / "fakebin", head)
    e["PATH"] = f"{extra_path or binder}:{e['PATH']}"
    script = ("set -uo pipefail\n"
              'RED=""; NC=""\n'
              'die() { echo -e "Error: $1" >&2; exit 1; }\n'
              f'FORCE={force}\nKEEP_BRANCH={keep_branch}\n'
              f'RESOLVED_BRANCH="{branch}"\nMAIN_REPO="{repo_dir}"\n') + _guard_block()
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)


def test_a_commit_added_after_the_pr_blocks_the_teardown(repo):
    r, at_pr, env = repo
    subprocess.run(["git", "-C", str(r), "commit", "-aqm", "after the PR",
                    "--allow-empty"], check=True, env=env)
    out = _run(r, env, head=at_pr)
    assert out.returncode != 0, out.stdout
    assert "never carried" in out.stderr
    assert "after the PR" in out.stderr, "the refusal must name what it is protecting"


def test_a_branch_the_pr_fully_carried_is_reapable(repo):
    r, at_pr, env = repo
    out = _run(r, env, head=at_pr)
    assert out.returncode == 0, out.stderr


def test_a_post_pr_commit_already_on_a_remote_does_not_block(repo, tmp_path):
    """The guard is about LOSS, not tidiness. `remove-worktree` deletes only the
    local branch, so a commit already pushed somewhere survives the teardown —
    refusing those would block the reaping this guard exists alongside, for work
    that is in no danger. This is the case that separates the two: same post-PR
    commit, but it has a home on a remote."""
    r, at_pr, env = repo
    subprocess.run(["git", "-C", str(r), "commit", "-aqm", "after the PR",
                    "--allow-empty"], check=True, env=env)

    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin", str(upstream)],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(r), "push", "-q", "origin", "feat/x"],
                   check=True, env=env)

    out = _run(r, env, head=at_pr)
    assert out.returncode == 0, out.stderr
    assert "never carried" not in out.stderr


def test_keep_branch_skips_the_guard_because_the_commits_survive(repo):
    r, at_pr, env = repo
    subprocess.run(["git", "-C", str(r), "commit", "-aqm", "after the PR",
                    "--allow-empty"], check=True, env=env)
    out = _run(r, env, head=at_pr, keep_branch="true")
    assert out.returncode == 0, out.stderr


def test_force_overrides_a_deliberate_operator(repo):
    r, at_pr, env = repo
    subprocess.run(["git", "-C", str(r), "commit", "-aqm", "after the PR",
                    "--allow-empty"], check=True, env=env)
    out = _run(r, env, head=at_pr, force="true")
    assert out.returncode == 0, out.stderr


def test_no_pr_for_the_branch_does_not_block(repo):
    """Cannot-tell must never block: a branch with no PR is the common case for
    a worktree torn down before it ever opened one."""
    r, _, env = repo
    out = _run(r, env, head=None)
    assert out.returncode == 0, out.stderr


def test_an_unknown_head_object_does_not_block_but_must_not_read_as_zero(repo):
    """`git log <absent>..<branch>` fails, and a bare `| wc -l` would call that 0
    — "nothing to lose" on exactly the branch that cannot be vouched for. The
    guard must bail out rather than conclude the branch is empty."""
    r, _, env = repo
    absent = "0" * 40
    out = _run(r, env, head=absent)
    assert out.returncode == 0, out.stderr
    assert "never carried" not in out.stderr


def test_no_gh_on_path_does_not_block(repo, tmp_path):
    r, at_pr, env = repo
    subprocess.run(["git", "-C", str(r), "commit", "-aqm", "after the PR",
                    "--allow-empty"], check=True, env=env)
    # A PATH with the tools the guard legitimately needs, and no `gh`. Emptying
    # PATH outright would remove bash and git too and prove nothing.
    bare = tmp_path / "nogh_bin"
    bare.mkdir()
    for tool in ("bash", "git", "timeout", "sed", "wc"):
        found = shutil.which(tool)
        if found:
            (bare / tool).symlink_to(found)
    assert shutil.which("gh", path=str(bare)) is None
    e = dict(env)
    e["PATH"] = str(bare)
    script = ("set -uo pipefail\n"
              'RED=""; NC=""\n'
              'die() { echo -e "Error: $1" >&2; exit 1; }\n'
              'FORCE=false\nKEEP_BRANCH=false\n'
              f'RESOLVED_BRANCH="feat/x"\nMAIN_REPO="{r}"\n') + _guard_block()
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)
    assert out.returncode == 0, out.stderr
