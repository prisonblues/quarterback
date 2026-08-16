"""The refusals — the reason the local actions are safe to offer at all.

Driven against real git repositories rather than a stubbed git: every one of
these checks is a claim about what git reports, so stubbing it would test the
stub. Only ``worktree-holder`` is stubbed, because its answer depends on a live
board and a set of session markers that a test has no business inventing.
"""

from __future__ import annotations

import subprocess

from mcp_server.board import local


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def advance_origin(work):
    """Put a commit on the remote that `work` does not have yet."""
    other = work.parent / "other"
    subprocess.run(["git", "clone", str(work.parent / "origin.git"), str(other)], check=True,
                   capture_output=True)
    git(other, "config", "user.email", "o@example.invalid")
    git(other, "config", "user.name", "Other")
    (other / "NEW").write_text("two\n")
    git(other, "add", "NEW")
    git(other, "commit", "-m", "second")
    git(other, "push", "origin", "main")
    git(work, "fetch", "origin")


# -- check_free --------------------------------------------------------


def test_a_held_worktree_is_refused_and_the_holder_is_named(git_repo, holder_stub):
    holder_stub(3, "held by zeus/other-agent")
    outcome = local.check_free(str(git_repo))
    assert not outcome.ok
    assert "zeus/other-agent" in outcome.message


def test_could_not_tell_is_refused_rather_than_read_as_free(git_repo, holder_stub):
    """A down board must not make somebody else's checkout look available."""
    holder_stub(4)
    outcome = local.check_free(str(git_repo))
    assert not outcome.ok and "could not tell" in outcome.message


def test_a_missing_worktree_holder_is_also_a_refusal(git_repo, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    outcome = local.check_free(str(git_repo))
    assert not outcome.ok and "not on PATH" in outcome.message


def test_a_free_worktree_passes(git_repo, holder_stub):
    holder_stub(0)
    assert local.check_free(str(git_repo)).ok


# -- check_clean / check_no_unpushed -----------------------------------


def test_a_dirty_tree_is_refused(git_repo):
    (git_repo / "README").write_text("changed\n")
    outcome = local.check_clean(str(git_repo))
    assert not outcome.ok and "dirty" in outcome.message


def test_an_untracked_file_counts_as_dirty(git_repo):
    (git_repo / "scratch.txt").write_text("x\n")
    assert not local.check_clean(str(git_repo)).ok


def test_a_path_that_is_not_a_checkout_is_reported_as_such(tmp_path):
    assert not local.check_clean(str(tmp_path)).ok


def test_unpushed_commits_are_refused(git_repo):
    (git_repo / "LOCAL").write_text("mine\n")
    git(git_repo, "add", "LOCAL")
    git(git_repo, "commit", "-m", "local only")
    outcome = local.check_no_unpushed(str(git_repo))
    assert not outcome.ok and "1 unpushed" in outcome.message


def test_no_upstream_is_a_refusal_not_a_pass(git_repo):
    """Commits on a branch that tracks nothing exist on exactly one disk."""
    git(git_repo, "checkout", "-b", "orphan")
    outcome = local.check_no_unpushed(str(git_repo))
    assert not outcome.ok and "no upstream" in outcome.message


def test_a_synced_branch_passes(git_repo):
    assert local.check_no_unpushed(str(git_repo)).ok


# -- pull --------------------------------------------------------------


def test_pull_fast_forwards_a_clean_synced_checkout(git_repo, holder_stub):
    holder_stub(0)
    advance_origin(git_repo)
    assert local.pull(str(git_repo)).ok
    assert (git_repo / "NEW").exists()


def test_pull_refuses_before_touching_git_when_the_worktree_is_held(git_repo, holder_stub):
    holder_stub(3)
    advance_origin(git_repo)
    outcome = local.pull(str(git_repo))
    assert not outcome.ok
    assert not (git_repo / "NEW").exists()  # nothing was written


def test_pull_refuses_a_dirty_tree(git_repo, holder_stub):
    holder_stub(0)
    advance_origin(git_repo)
    (git_repo / "README").write_text("mine\n")
    assert not local.pull(str(git_repo)).ok
    assert not (git_repo / "NEW").exists()


def test_pull_refuses_when_the_checkout_holds_unpushed_commits(git_repo, holder_stub):
    holder_stub(0)
    (git_repo / "LOCAL").write_text("mine\n")
    git(git_repo, "add", "LOCAL")
    git(git_repo, "commit", "-m", "local only")
    advance_origin(git_repo)
    outcome = local.pull(str(git_repo))
    assert not outcome.ok and "unpushed" in outcome.message


# -- cherry_pick -------------------------------------------------------


def test_cherry_pick_applies_a_commit_the_checkout_can_reach(git_repo, holder_stub):
    holder_stub(0)
    advance_origin(git_repo)
    sha = subprocess.run(
        ["git", "rev-parse", "origin/main"], cwd=git_repo, capture_output=True, text=True
    ).stdout.strip()
    outcome = local.cherry_pick(str(git_repo), sha)
    assert outcome.ok, outcome.message
    assert (git_repo / "NEW").exists()


def test_cherry_pick_refuses_a_short_sha(git_repo, holder_stub):
    holder_stub(0)
    assert not local.cherry_pick(str(git_repo), "abc12").ok


def test_cherry_pick_reports_an_unreachable_sha_rather_than_guessing(git_repo, holder_stub):
    holder_stub(0)
    outcome = local.cherry_pick(str(git_repo), "0" * 40)
    assert not outcome.ok and "not in this checkout" in outcome.message


def test_a_conflicting_pick_leaves_no_half_applied_state_behind(git_repo, holder_stub):
    """An abandoned CHERRY_PICK_HEAD is a trap for whoever opens this next."""
    holder_stub(0)
    advance_origin(git_repo)
    sha = subprocess.run(
        ["git", "rev-parse", "origin/main"], cwd=git_repo, capture_output=True, text=True
    ).stdout.strip()
    (git_repo / "NEW").write_text("conflicting content\n")
    git(git_repo, "add", "NEW")
    git(git_repo, "commit", "-m", "conflict")
    outcome = local.cherry_pick(str(git_repo), sha)
    assert not outcome.ok
    assert not (git_repo / ".git" / "CHERRY_PICK_HEAD").exists()


def test_cherry_pick_refuses_a_held_worktree(git_repo, holder_stub):
    holder_stub(3)
    assert not local.cherry_pick(str(git_repo), "0" * 40).ok


# -- registry filtering ------------------------------------------------


def test_only_this_devices_worktrees_that_still_exist_are_offered(tmp_path):
    """The registry is a snapshot; a directory in it may have been removed since."""
    here = tmp_path / "live"
    here.mkdir()
    registered = [
        {"device": "zeus", "path": str(here)},
        {"device": "zeus", "path": str(tmp_path / "gone")},
        {"device": "atlas", "path": str(here)},
        {"device": "zeus", "path": None},
    ]
    assert local.local_worktrees(registered, "zeus") == [{"device": "zeus", "path": str(here)}]
