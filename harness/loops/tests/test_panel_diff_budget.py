"""How much diff each model is given, and what a bad budget does.

One hardcoded 60k used to stand in for every reviewer's context window. The
budget is now config, which means it can also be configured WRONG — and a wrong
budget is invisible in the output: the reviewer reads a prefix and reports
confidently on the part it saw.

So the config wins and the consequence is surfaced — truncation is reported per
reviewer, with the budget that cut it. Only a value that cannot be a budget at
all (not a number, or <= 0) falls back, and it says so.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402


def budget(block, key="max_diff_chars", fallback=panel.MAX_DIFF_CHARS):
    notes: list[str] = []
    return panel.diff_budget(block, key, fallback, notes), notes


def test_unset_inherits_the_fallback():
    """The fallback chain is repo default -> reviewer override, so 'unset' has to
    mean 'inherit' rather than 'zero'."""
    assert budget({})[0] == panel.MAX_DIFF_CHARS
    assert budget({"max_diff_chars": None})[0] == panel.MAX_DIFF_CHARS
    assert budget({"max_diff_chars": ""})[0] == panel.MAX_DIFF_CHARS


def test_a_set_budget_is_used_including_a_string_from_json():
    assert budget({"max_diff_chars": 200_000})[0] == 200_000
    assert budget({"max_diff_chars": "200000"})[0] == 200_000


def test_junk_falls_back_and_says_so():
    """Silently honouring it truncates the review to nothing; silently dropping
    it leaves you believing a budget you never got."""
    for junk in ("lots", [], {"chars": 1}, True):
        value, notes = budget({"max_diff_chars": junk})
        assert value == panel.MAX_DIFF_CHARS
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
        assert value == panel.MAX_DIFF_CHARS
        assert notes and "no diff at all" in notes[0]


def test_notes_accumulate_across_reviewers():
    """Every bad key is reported, not just the first — one report, one pass."""
    notes: list[str] = []
    panel.diff_budget({"max_diff_chars": "nope"}, "max_diff_chars", 60_000, notes)
    panel.diff_budget({"max_diff_chars": 0}, "max_diff_chars", 60_000, notes)
    assert len(notes) == 2
