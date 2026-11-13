"""remove-worktree must not report a teardown that removed nothing as complete (#808).

The branch name is the script's only argument and it says nothing about which repo
it belongs to, while `MAIN_REPO` is whatever checkout the shell is in. Three
teardowns run from the wrong checkout each found nothing at all eight steps and
each printed "Cleanup complete", over worktrees that were still registered, on
disk, with their containers up and their claims held.

The same defect in the steps themselves: a directory the delete could not remove,
a branch that could not be deleted, and an nginx that did not restart were each a
yellow note, and the summary still said "Cleanup complete" over them.

These drive the real script against throwaway repos, as the branch-guard suite
does, and reuse its fixture and sandbox.

Run: pytest harness/tests
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_remove_worktree_branch_guard import add_worktree, branches, git, repo, run_remove  # noqa: E402,F401


@pytest.fixture
def other_repo(tmp_path):
    """A second, unrelated checkout — the one the shell had wandered into."""
    other = tmp_path / "elsewhere"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(other)], check=True)
    git(other, "config", "user.email", "t@example.com")
    git(other, "config", "user.name", "t")
    (other / "README.md").write_text("other\n")
    git(other, "add", "-A")
    git(other, "commit", "--quiet", "-m", "init")
    return other


def test_a_name_with_nothing_behind_it_in_this_repo_is_refused(repo, tmp_path,
                                                               other_repo):
    wt = add_worktree(repo, "fix-issue-2032", "fix/issue-2032")

    proc = run_remove(other_repo, "fix-issue-2032", tmp_path)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Cleanup complete" not in proc.stdout
    assert str(other_repo) in proc.stderr and "Nothing was removed" in proc.stderr
    # And the worktree it was meant for is exactly as it was.
    assert wt.is_dir() and "fix/issue-2032" in branches(repo)


def test_the_slash_spelling_is_refused_the_same_way(repo, tmp_path, other_repo):
    add_worktree(repo, "fix-issue-2032", "fix/issue-2032")
    proc = run_remove(other_repo, "fix/issue-2032", tmp_path)
    assert proc.returncode == 2 and "Cleanup complete" not in proc.stdout


def test_a_tree_deleted_by_hand_still_has_its_leftovers_cleaned(repo, tmp_path):
    """A missing directory is not "nothing here": the branch is still to delete."""
    wt = add_worktree(repo, "fix-issue-43", "fix/issue-43")
    subprocess.run(["rm", "-rf", str(wt)], check=True)

    proc = run_remove(repo, "fix/issue-43", tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "removing what is left" in proc.stdout
    assert "fix/issue-43" not in branches(repo)


def test_a_directory_the_delete_could_not_remove_is_a_warning(repo, tmp_path):
    wt = add_worktree(repo, "fix-issue-43", "fix/issue-43")
    stuck = wt / "owned-by-a-container"
    stuck.mkdir()
    (stuck / "file").write_text("x")
    stuck.chmod(0o500)  # nothing inside it can be unlinked
    try:
        proc = run_remove(repo, "fix-issue-43", tmp_path)
    finally:
        stuck.chmod(0o700)

    assert wt.is_dir()
    assert "Cleanup complete" not in proc.stdout
    assert "WITH WARNINGS" in proc.stdout and "is still there" in proc.stdout


def test_a_branch_that_could_not_be_deleted_is_a_warning(repo, tmp_path):
    add_worktree(repo, "fix-issue-43", "fix/issue-43")
    # A held ref lock is what makes `git branch -D` fail for real.
    lock = repo / ".git" / "refs" / "heads" / "fix" / "issue-43.lock"
    lock.write_text("")
    try:
        proc = run_remove(repo, "fix-issue-43", tmp_path)
    finally:
        lock.unlink()

    assert "fix/issue-43" in branches(repo)
    assert "Cleanup complete" not in proc.stdout
    assert "could not delete branch" in proc.stdout
