"""How much diff each model is given, and what a bad budget does.

There is no budget by default any more: the whole diff goes to every reviewer,
because the 60k that used to be here was inherited from the argv ceiling and
outlived it, and a reviewer reading a prefix reports confidently on the part it
saw. A repo can still ask for a cap, which means it can also ask WRONG.

So the config wins and the consequence is surfaced — truncation is reported per
reviewer, with the budget that cut it. Only a value that cannot be a budget at
all (not a number, or <= 0) falls back, and it says so.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


def budget(block, key="max_diff_chars", fallback=panel.DEFAULT_DIFF_BUDGET):
    notes: list[str] = []
    return panel.diff_budget(block, key, fallback, notes), notes


def test_nothing_configured_means_the_whole_diff():
    """The default is no cap at all. A number here would be a guess about every
    reviewer's context window, and the cost of guessing low is invisible."""
    assert panel.DEFAULT_DIFF_BUDGET is None
    assert budget({})[0] is None


def test_unset_inherits_the_fallback():
    """The fallback chain is repo default -> reviewer override, so 'unset' has to
    mean 'inherit' rather than 'zero'."""
    assert budget({})[0] == panel.DEFAULT_DIFF_BUDGET
    assert budget({"max_diff_chars": None})[0] == panel.DEFAULT_DIFF_BUDGET
    assert budget({"max_diff_chars": ""})[0] == panel.DEFAULT_DIFF_BUDGET


def test_a_set_budget_is_used_including_a_string_from_json():
    assert budget({"max_diff_chars": 200_000})[0] == 200_000
    assert budget({"max_diff_chars": "200000"})[0] == 200_000


def test_junk_falls_back_and_says_so():
    """Silently honouring it truncates the review to nothing; silently dropping
    it leaves you believing a budget you never got."""
    for junk in ("lots", [], {"chars": 1}, True):
        value, notes = budget({"max_diff_chars": junk})
        assert value == panel.DEFAULT_DIFF_BUDGET
        assert notes and "not a number" in notes[0]


def test_a_small_budget_is_honoured_not_second_guessed():
    """There is no lower sanity bound, on purpose. One was tried and it was
    decoration: the plausible slip is a dropped zero (60_000 -> 6_000), which
    clears any floor you'd actually write. What surfaces a too-small budget is
    the truncation line naming the reviewer and the budget that cut it — not the
    panel overriding a number someone explicitly wrote."""
    assert budget({"max_diff_chars": 600}) == (600, [])
    assert budget({"max_diff_chars": 6_000}) == (6_000, [])


def test_a_budget_that_would_send_no_diff_is_refused():
    """<= 0 cannot be a budget: it produces a confident review of nothing."""
    for empty in (0, -1, "0"):
        value, notes = budget({"max_diff_chars": empty})
        assert value == panel.DEFAULT_DIFF_BUDGET
        assert notes and "no diff at all" in notes[0]


def test_a_junk_budget_falls_back_to_the_whole_diff_in_words():
    """The note has to name what you got. With no inherited number to print, the
    formatting that says "using 60,000" has nothing to interpolate — and a
    traceback while explaining a config mistake is a poor way to explain it."""
    value, notes = budget({"max_diff_chars": "lots"}, fallback=None)
    assert value is None
    assert notes and "the whole diff" in notes[0]
    value, notes = budget({"max_diff_chars": -1}, fallback=None)
    assert value is None and "the whole diff" in notes[0]


def test_notes_accumulate_across_reviewers():
    """Every bad key is reported, not just the first — one report, one pass."""
    notes: list[str] = []
    panel.diff_budget({"max_diff_chars": "nope"}, "max_diff_chars", 60_000, notes)
    panel.diff_budget({"max_diff_chars": 0}, "max_diff_chars", 60_000, notes)
    assert len(notes) == 2
