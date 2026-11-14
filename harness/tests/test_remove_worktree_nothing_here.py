"""remove-worktree must not report a teardown that removed nothing as complete (#808).

The branch name is the script's only argument and it says nothing about which repo
it belongs to, while `MAIN_REPO` is whatever checkout the shell is in. Three
teardowns run from the wrong checkout each found nothing at all eight steps and
each printed "Cleanup complete", over worktrees that were still registered, on
disk, with their containers up and their claims held.

The outcome is read from what the steps removed, not from a probe made before
them. The first cut of this fix used a probe, and one review round found it had
drifted from the steps three ways (#841 round 1). So each test below leaves
exactly ONE artefact of a worktree behind and checks that the teardown finds it.
A test with two would pass with one detector broken.

The same defect inside the steps: a directory the delete could not remove, a
branch that could not be deleted, and an nginx that did not restart or was not
there were yellow notes, and the summary still said "Cleanup complete".

These drive the real script against throwaway repos, as the branch-guard suite
does, and reuse its fixture and sandbox.

Run: pytest harness/tests
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402
from test_remove_worktree_branch_guard import (  # noqa: E402,F401
    REMOVE,
    TOOLS,
    add_worktree,
    branches,
    git,
    repo,
    run_remove,
)

BASH = shutil.which("bash") or "/bin/bash"
#: What the board answers a release for a claim nobody holds — which is what a
#: teardown run from the wrong repository gets, since the repo is part of the key.
NOTHING_HELD = {"qb-release": "echo '{\"released\": []}'"}


def run_with(repo_dir, args, tmp_path, fakes: dict[str, str]):
    """The real script with fake tools ahead of the sandbox toolbox on PATH."""
    fake_dir = tmp_path / "fakes"
    fake_dir.mkdir(exist_ok=True)
    for name, body in fakes.items():
        tool = fake_dir / name
        tool.write_text(f"#!{BASH}\n{body}\n")
        tool.chmod(0o755)
    return subprocess.run(
        [str(REMOVE), *args], cwd=repo_dir, capture_output=True, text=True,
        env=_path_sandbox.sandbox_env(tmp_path, fake_dir, tools=TOOLS))


def complete(proc) -> bool:
    """A clean teardown that removed something: exit 0 and no warning. Asserted
    rather than the exit code alone, because a warning also exits 0 and would
    let a broken `removed()` pass for a working one."""
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return "Cleanup complete" in proc.stdout


def registered(repo_dir, path) -> bool:
    out = git(repo_dir, "worktree", "list", "--porcelain").stdout
    return f"worktree {path}" in out.splitlines()


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


# ------------------------------------------------------------ nothing here at all

@pytest.mark.parametrize("name", ["fix-issue-2032", "fix/issue-2032"])
def test_a_name_with_nothing_behind_it_in_this_repo_is_refused(repo, tmp_path,
                                                               other_repo, name):
    wt = add_worktree(repo, "fix-issue-2032", "fix/issue-2032")

    proc = run_with(other_repo, [name], tmp_path, NOTHING_HELD)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Cleanup complete" not in proc.stdout
    assert "Nothing removed" in proc.stderr and str(other_repo) in proc.stderr
    # And the worktree it was meant for is exactly as it was.
    assert wt.is_dir() and "fix/issue-2032" in branches(repo)


# -------------------------------------------- one artefact left, and it is found

def test_only_a_registration_left_is_pruned(repo, tmp_path):
    wt = add_worktree(repo, "fix-issue-43", "fix/issue-43")
    subprocess.run(["rm", "-r", "-f", str(wt)], check=True)
    git(repo, "branch", "-D", "fix/issue-43")  # the branch is not the signal here

    proc = run_with(repo, ["fix-issue-43"], tmp_path, NOTHING_HELD)

    assert complete(proc) and not registered(repo, wt)


def test_only_a_branch_left_is_deleted(repo, tmp_path):
    git(repo, "branch", "fix/issue-43")

    proc = run_with(repo, ["fix/issue-43"], tmp_path, NOTHING_HELD)

    assert complete(proc) and "fix/issue-43" not in branches(repo)


def test_a_flattened_name_never_deletes_a_branch_it_did_not_name(repo, tmp_path):
    """#841 round 2's P1: a lookup from `fix-issue-43` to `fix/issue-43` deleted a
    branch with unpushed commits, past the post-PR guard, which only ever guards
    the branch a live directory names. With no directory there is no such guard,
    so the argument is the only branch the teardown may touch."""
    git(repo, "checkout", "--quiet", "-b", "fix/issue-43")
    (repo / "work.txt").write_text("unpushed\n")
    git(repo, "add", "work.txt")
    git(repo, "commit", "--quiet", "-m", "unpushed work")
    git(repo, "checkout", "--quiet", "wip/current")

    proc = run_with(repo, ["fix-issue-43"], tmp_path, NOTHING_HELD)

    assert "fix/issue-43" in branches(repo)
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_a_teardown_does_not_prune_another_worktrees_registration(repo, tmp_path):
    """Prune is repo-wide. Another tree whose directory is briefly missing (an
    unmounted volume) keeps its registration through an unrelated teardown."""
    other = add_worktree(repo, "feat-elsewhere", "feat/elsewhere")
    target = add_worktree(repo, "fix-issue-43", "fix/issue-43")
    parked = other.with_name(other.name + ".unmounted")
    other.rename(parked)
    try:
        proc = run_with(repo, ["fix-issue-43"], tmp_path, NOTHING_HELD)
        assert complete(proc) and not target.exists()
        assert registered(repo, other)
    finally:
        parked.rename(other)


def test_only_a_port_entry_left_is_dropped(repo, tmp_path):
    (repo / ".worktree-ports").write_text("5698:fix/issue-43\n5699:other\n")

    proc = run_with(repo, ["fix-issue-43"], tmp_path, NOTHING_HELD)

    assert complete(proc)
    assert (repo / ".worktree-ports").read_text() == "5699:other\n"


def test_only_a_claim_left_is_handed_back(repo, tmp_path):
    """qb-reconcile tells people to run remove-worktree once the tree is gone, to
    hand its claim back. That recovery must not be refused."""
    proc = run_with(repo, ["fix-issue-43"], tmp_path, {
        "qb-release": "echo '" + json.dumps({"released": [{"kind": "issue"}]}) + "'"})

    assert complete(proc)


def test_a_claim_that_was_not_held_is_not_something_removed(repo, tmp_path,
                                                            other_repo):
    proc = run_with(other_repo, ["fix-issue-43"], tmp_path, NOTHING_HELD)
    assert proc.returncode == 2


def test_only_containers_left_are_removed(repo, tmp_path):
    proc = run_with(repo, ["fix-issue-43"], tmp_path, {**NOTHING_HELD, "docker": '''
case "$1" in
  ps) echo "proj-fix-issue-43" ;;
  rm) exit 0 ;;
esac'''})

    assert complete(proc)


def test_the_real_qb_release_saying_nothing_is_held_still_ends_in_exit_2(tmp_path,
                                                                          other_repo):
    """Against the REAL tool, not a fake of it: its "nothing to release" paths print
    no JSON at all, which a fake that always prints some never shows. A branch
    naming no issue takes one of them without needing a board."""
    proc = subprocess.run(
        [str(REMOVE), "wip-notes"], cwd=other_repo, capture_output=True, text=True,
        env=_path_sandbox.sandbox_env(tmp_path, tools=(*TOOLS, "python3")))
    assert "names no issue" in proc.stderr, proc.stdout + proc.stderr
    assert proc.returncode == 2, proc.stdout + proc.stderr


@pytest.mark.parametrize("answer", ["not json", '{"released": "x"}', '{"other": []}'])
def test_a_claim_answer_that_cannot_be_read_is_a_warning(repo, tmp_path, other_repo,
                                                         answer):
    """Exit 0 from qb-release with output that is not a `released` array says nothing
    about whether a claim went, so it is neither "removed" nor nothing."""
    proc = run_with(other_repo, ["fix-issue-43"], tmp_path,
                    {"qb-release": f"echo '{answer}'"})
    assert proc.returncode == 0 and "WITH WARNINGS" in proc.stdout


def test_a_kept_branch_is_not_something_removed(repo, tmp_path):
    """--keep-branch keeps the one artefact there is, so nothing was removed."""
    git(repo, "branch", "fix/issue-43")
    proc = run_with(repo, ["--keep-branch", "fix/issue-43"], tmp_path, NOTHING_HELD)
    assert proc.returncode == 2 and "fix/issue-43" in branches(repo)


# ------------------------------------------------------- a step that fell short

@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not stop root")
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


@pytest.fixture
def nginx_repo(repo):
    """The repo with an nginx config holding exactly one worktree block."""
    (repo / ".worktree.json").write_text(json.dumps({"nginx": {"config": "nginx.conf"}}))
    # create-worktree's own spelling: no space after the colon.
    (repo / "nginx.conf").write_text("http {\n    # WORKTREE-START:fix-issue-43\nx;\n"
                                     "    # WORKTREE-END:fix-issue-43\n}\n")
    return repo


@pytest.mark.parametrize("docker, says", [
    ('case "$1" in ps) echo proj-nginx ;; restart) exit 1 ;; esac',
     "could not restart nginx"),
    ('exit 0', "found no nginx container"),
])
def test_an_nginx_that_was_not_restarted_is_a_warning(nginx_repo, tmp_path, docker,
                                                      says):
    proc = run_with(nginx_repo, ["fix-issue-43"], tmp_path,
                    {"docker": docker, **NOTHING_HELD})

    assert "WORKTREE-START" not in (nginx_repo / "nginx.conf").read_text()
    assert "WITH WARNINGS" in proc.stdout and says in proc.stdout


def test_no_restart_is_attempted_when_no_block_was_removed(repo, tmp_path):
    (repo / ".worktree.json").write_text(json.dumps({"nginx": {"config": "nginx.conf"}}))
    (repo / "nginx.conf").write_text("http {}\n")
    git(repo, "branch", "fix/issue-43")
    proc = run_with(repo, ["fix/issue-43"], tmp_path, {"docker": "exit 1", **NOTHING_HELD})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WITH WARNINGS" not in proc.stdout and "skipping restart" in proc.stdout
