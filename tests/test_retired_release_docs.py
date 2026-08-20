"""The release allocator is gone, and neither of its two documents may say otherwise.

#172 deleted `POST /release/claim`, `POST /release/reclaim`, `GET /releases`,
`kind='release'` and their tools, on the argument that `release_stamp.py` had been
doing the job since v2.34 while the allocator's own rows went stale for every open
PR. Deleting the code left two files describing it as live, and they are the worst
two possible:

* `app/models/resource_lease.py` — the file a reader opens to find out what lives
  in `resource_leases`. Its docstring listed `kind="release"` as one of two kinds
  the table carries, which made the one kind nothing can write the most prominent
  thing on the page.
* `scripts/release_stamp.py` — whose whole argument is that it is now the only
  answer. Its docstring still explained `POST /release/claim` as an endpoint that
  "records that a caller INTENDS to take a number".

A stale record of a deleted mechanism is the same defect #172 is about, one layer
up: a second answer to a question that has one. There is no way to grep for "is
described in the present tense", so this pins the sentences that were wrong and
the sentence that replaced them — crude, and enough to make a revert visible.
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


def test_release_stamp_does_not_describe_the_allocator_as_live():
    """`release_stamp.py` is the whole mechanism now. Its docstring is where an
    agent goes to learn how release numbers work, so it is the one place that must
    not send them to an endpoint that 404s."""
    text = (REPO_ROOT / "scripts/release_stamp.py").read_text()

    assert "records that a caller INTENDS to take a number" not in text, \
        "the allocator is described in the present tense again"
    assert "the allocator is deleted" in text, \
        "the docstring must say the other mechanism is gone, not merely unused"
