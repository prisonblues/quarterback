"""Deleted release mechanisms, pinned so a revert is visible.

There is no way to grep for "is described in the present tense", and this repo has now lost
two mechanisms whose documentation outlived them. So each one gets a pin: the sentence that
was wrong, and the sentence that replaced it.

* **The board allocator (#172).** `POST /release/claim`, `POST /release/reclaim`,
  `GET /releases` and `kind='release'` were deleted because they recorded an INTENTION to
  take a number that nothing read, and the rows went stale for every PR that stayed open.
  `app/models/resource_lease.py` — the file a reader opens to find out what lives in
  `resource_leases` — went on listing `kind="release"` as one of two kinds the table carries,
  which made the one kind nothing can write the most prominent thing on the page.

* **Branch-side stamping and its push-time tag reservation (#122, #296).** `release_stamp.py
  apply` ran on a branch and every brief in the repo told a worker to run it. Every worker
  did, correctly, as instructed: three of six open pull requests were `CONFLICTING` on the
  same two files, and a squash merge orphaned `refs/tags/v3.8` against a `chore(release)`
  commit it discarded (#406). The affordance was the bug, so the script is gone rather than
  documented against — and the test below pins its ABSENCE, because a sentence saying "do not
  run this" is satisfied by a repo where running it still works.

A stale record of a deleted mechanism is the same defect either way: a second answer to a
question that has one.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_lease_table_does_not_list_a_kind_nothing_can_write():
    """The table still carries `merge` and `work`. It never carries `release`, and
    the file that documents the table has to be the first place that says so."""
    text = (REPO_ROOT / "app/models/resource_lease.py").read_text()

    assert "held while a branch owns a" not in text, \
        "the release bullet is back in the table's own docstring"
    assert "There is no release kind here any more" in text, \
        "the docstring must say what happened to kind='release'"
    # And the kinds that ARE there are still named — a docstring that answered
    # only the negative would leave the reader worse off than the stale one.
    assert '``kind="work"``' in text and '``kind="merge"``' in text


def test_no_branch_side_stamping_affordance_is_still_reachable():
    """The mechanism check, and the one that keeps #122 from regressing.

    `release_stamp.py apply` was *documented* as the right thing to run before a merge, so
    every agent ran it, correctly, as instructed — and produced three conflicting pull
    requests and an orphaned tag in one night. The affordance was the bug. This pins the
    removal itself rather than a sentence about it: a script that is back is a script an
    agent can be told to run.
    """
    assert not (REPO_ROOT / "scripts/release_stamp.py").exists(), \
        "the branch-side stamper is back; #122 deleted it rather than documenting against it"
    assert (REPO_ROOT / "scripts/release.py").exists(), \
        "and its replacement has to be here — a repo with neither cannot cut a release"

    text = (REPO_ROOT / "scripts/release.py").read_text()
    for gone in ("release.py apply", "release.py preflight", "release.py collision"):
        assert gone not in text, f"`{gone}` is described as runnable again"


def test_the_release_tool_says_where_a_branch_writes_instead():
    """A refusal that does not name the fragment path gets retried or worked around, and both
    are worse than the original mistake. The path is spelled once, in `FRAGMENT_PATH`, and it
    has to reach the reader of the module as well as the reader of the refusal."""
    text = (REPO_ROOT / "scripts/release.py").read_text()

    assert "changelog.d/<issue>.<kind>.md" in text
    assert (REPO_ROOT / "changelog.d" / "README.md").exists(), \
        "the contract the refusal points at has to exist"


def test_the_release_tool_does_not_describe_a_push_time_reservation_as_live():
    """`release_tag.py reserve` was the lock a branch-side stamp needed. Branches do not
    stamp, there is no race on `main`, and a docstring still explaining how to take a number
    at push time would send the next agent to a subcommand that is not there — the same
    defect as #172's, one mechanism later."""
    text = (REPO_ROOT / "scripts/release_tag.py").read_text()

    assert "release_tag.py reserve " not in text, \
        "the deleted `reserve` subcommand is listed as a command again"
    assert "There is nothing to reserve any more (#122)" in text, \
        "the docstring must say what happened to it, not merely go quiet"
