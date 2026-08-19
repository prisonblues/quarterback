"""The panel already knew whether CI passed, and told no reviewer (#91).

`review_ci()` has run on every round since it was written, and its result reached
the payload and the human report — never a prompt. So seats judged a diff while a
full suite had already passed or failed on that exact commit, and spent
`could_not_assess` entries saying they could not run anything.

That is not free. Each such declaration becomes a `coverage_veto` line and
`round_stop` computes `confident` as `not veto` — so a seat's inability to run the
tests cost the whole ROUND its confident stop, with the answer already in hand.

What these pin is mostly the negative space, because the failure mode is a
reviewer being told something reassuring and wrong:

* the four non-passing states must never read as a pass;
* a pass must not read as "the code is correct";
* the section must be present even when CI could not be read, so that "not
  known" cannot be mistaken for "fine" by its absence.

Run: pytest harness/loops/tests
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh` is defined here since #129
from conftest import gh_stub  # noqa: E402

#: Every state `review_ci` can return. Named here so a new one added to the
#: reader without a matching branch in `ci_brief` fails the sweep below rather
#: than silently falling into the catch-all and reading as "unknown".
STATES = ("PASS", "FAIL", "PENDING", "none", "unknown")


def test_every_state_says_which_one_it_is():
    """The five states are five different facts. A reviewer told the wrong one is
    worse off than a reviewer told nothing."""
    seen = {s: panel.ci_brief(s, []) for s in STATES}
    assert len({v for v in seen.values()}) == len(STATES), "two states render alike"
    for state, text in seen.items():
        assert text.strip(), state


def test_no_state_but_PASS_can_be_read_as_a_pass():
    """The acceptance criterion this issue turns on. PENDING, none and unknown are
    all "no green suite here", and each has been mistaken for one before."""
    for state in ("PENDING", "none", "unknown"):
        text = panel.ci_brief(state, [])
        assert "not a pass" in text.lower(), f"{state} does not deny being a pass: {text}"
        assert "PASSED" not in text, state


def test_a_pass_says_what_it_MEANS_and_not_that_the_code_works():
    """Green CI is not a licence to stop looking, and this repo's whole argument is
    that a passing signal is the dangerous kind. The prompt has to say that a pass
    means "every test we thought to write passed" — the defects a reviewer is
    hunting live exactly where nobody wrote one."""
    text = panel.ci_brief("PASS", [])
    assert "thought to write" in text
    assert "NOT evidence the code is correct" in text


def test_a_pass_names_the_finding_class_it_refutes():
    """The point of handing it over: "this new test never runs" / "this may not even
    import" are confident findings about runtime behaviour the seat cannot check,
    and a green suite refutes them for free."""
    text = panel.ci_brief("PASS", [])
    assert "never runs" in text and "import" in text


def test_a_failure_names_the_failing_checks():
    """The list is already returned by `review_ci` and was already dropped."""
    text = panel.ci_brief("FAIL", ["app suite", "harness suites"])
    assert "app suite" in text and "harness suites" in text


def test_a_failure_with_no_names_still_reads_as_a_failure():
    """`failing` can be empty when the checks API answered thinly. The state is
    still FAIL and must not degrade into something vaguer."""
    text = panel.ci_brief("FAIL", [])
    assert "FAILED" in text
    assert "unavailable" in text


def test_an_unreadable_ci_gives_its_reason_and_still_appears():
    """A run that could not read CI says so rather than omitting the section — an
    absent section reads as "nothing to say", which is the reading this must not
    allow."""
    text = panel.ci_brief("unknown", [], "gh exited 1")
    assert "could NOT be read" in text and "gh exited 1" in text


# --------------------------------------------------------------------------
# ...and that it actually reaches both prompts
# --------------------------------------------------------------------------

def test_both_prompts_have_a_slot_for_it():
    """Rendering with the slot missing is a KeyError, so this is the guard that
    the wiring did not get reverted while the helper survived."""
    for name, fields in (("REVIEW_PROMPT", {"n": 1, "repo": "a/b", "base": "main",
                                            "ci": "CI-MARKER", "diff": "",
                                            "code": ""}),
                         ("JUDGE_PROMPT", {"findings": "", "coverage": "",
                                           "ci": "CI-MARKER", "diff": ""})):
        rendered = getattr(panel, name).format(**fields)
        assert "CI-MARKER" in rendered, name


def test_the_reviewers_prompt_carries_the_real_result(monkeypatch, tmp_path):
    """End to end through `run()`: the seat's prompt must contain what CI said.

    This is the assertion that would have failed for the whole life of the panel
    before #91 — `review_ci` ran on every round and its answer reached no prompt.
    """
    prompts = []

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "acme/board", "path": "/tmp/r", "review_panel": {},
        "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}})
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_ci",
                        lambda *a: ("FAIL", ["app suite"], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))

    def fake_review(name, model, prompt, effort="", **_kw):  # **_kw: code_tree since #113
        prompts.append(prompt)
        return panel.ReviewerRun([], None, 800, None)

    monkeypatch.setattr(panel, "review_llm", fake_review)
    assert panel.run("board", 34, post=False, record=False) == 0

    assert prompts, "no seat was dispatched"
    assert all("FAILED" in p and "app suite" in p for p in prompts), \
        "the seat was not told what CI said"


def test_the_seat_is_told_before_it_is_dispatched(monkeypatch, tmp_path):
    """Ordering, not just content. CI used to be collected AFTER the reviewers
    ran, which is why its result could not be in their prompt — so this pins that
    the read happens first rather than that a value happened to be around."""
    order = []

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        "github": "acme/board", "path": "/tmp/r", "review_panel": {},
        "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}})
    monkeypatch.setattr(panel_core, "sh", gh_stub(diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ""))

    def fake_ci(*a):
        order.append("ci")
        return ("PASS", [], None)

    def fake_review(name, model, prompt, effort="", **_kw):  # **_kw: code_tree since #113
        order.append("seat")
        return panel.ReviewerRun([], None, 800, None)

    monkeypatch.setattr(panel, "review_ci", fake_ci)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    assert panel.run("board", 34, post=False, record=False) == 0
    assert order and order[0] == "ci", f"CI must be read before any seat runs: {order}"
