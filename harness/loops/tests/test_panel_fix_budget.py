"""#622: the churn budget, counted by something that is not the fix pass.

`low_severity_fix_lines` is a hard cap on what one round may spend clearing findings
below the `round_trigger_floor` cut. Every other bound on a fix pass is measured from
outside it — `max_fix_growth` by the caller, `unrefereed_fix` and `guard_churn` off the
fix range's own split, `fix_injection` off provenance — and this one was measured by the
fix pass. The dial is resolved in `panel.py`, relayed into the fixer's brief as a
paragraph ("measure each fix's churned lines (`git diff --numstat`) … stop when the
budget is spent"), and read by nobody else.

On `prisonblues/lexray#1780` the relayed number was correct at every round and the fix
passes came out at 850, 322, 356 and 142 added lines against a budget of 40. Nothing
anywhere recorded that. `harness_rules.py` already states the principle beside the dial
— the fixer "is never asked 'does this risk ballooning?', because that is a judgement
by the actor whose judgement the 85% impugns" — and the counting had been handed to the
same actor.

Three things are pinned here:

* the MEASUREMENT — the pass priced exactly as the brief prices it, off the same split
  `unrefereed_fix` and `guard_churn` read, so a repo's budget and its ceiling can never
  count different lines;
* the ONE-SIDEDNESS — the budget bounds the 💸 band and not the pass, and a diff cannot
  say which line paid for which finding. `within: true` is a fact about the pass;
  `within: false` is the ABSENCE of that fact and must never render as a breach. This is
  the assertion most likely to be lost by someone tightening the wording;
* the SILENCE — it gates nothing, it arms nothing, and it adds no dial. #67 for the
  first, and #621's "not a 29th dial" for the rest.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
import panel_rounds  # noqa: E402
import harness_rules  # noqa: E402
from conftest import gh_stub  # noqa: E402
# The band fixture is `test_panel_dials`' — two floors a tier apart, so there IS a
# budgeted band and the 💸 note renders. Imported rather than rebuilt: the sentence this
# file asserts on lives inside that note, and a second fixture drifting from the first
# would let the note change shape with this test still green.
from test_panel_dials import band_run, finding

DEFAULT_BLOCK = harness_rules.DEFAULTS["review_panel"]


def _split(production=0, test=0, prose=0):
    """One fix pass as `referee_state` reads it, which is what the budget reads."""
    return panel_rounds.referee_state(
        {"production": production, "test": test, "prose": prose}, False)


def _spend(production=0, test=0, prose=0, limit=40, weight=2, band=True,
           measured=True):
    """One pass priced against a budget, as `round_stop` receives it."""
    return panel_rounds.fix_budget_state(
        _split(production, test, prose) if measured else None,
        limit, weight, band)


# -------------------------------------------------------------------- the measurement

def test_the_pass_is_priced_the_way_the_BRIEF_prices_it():
    """Production at 1, test and prose at `unrefereed_line_weight`, over churn. The
    fixer is told that sentence and now so is the payload — one arithmetic, stated in
    two places, applied by the half that is not the actor."""
    got = _spend(production=10, test=8, prose=2, weight=2)
    assert got["production"] == 10 and got["unrefereed"] == 10
    assert got["spend"] == 30 and got["weight"] == 2


def test_it_reads_the_SAME_SPLIT_its_two_neighbours_read():
    """`low_severity_fix_lines`, `max_fix_guard_lines` and #554's predicate are three
    readings of ONE split. A second derivation here would let the report print two
    different numbers for the same lines, which is the failure `guard_churn_state`
    already refuses one function up."""
    referee = _split(production=177, test=330, prose=50)
    got = panel_rounds.fix_budget_state(referee, 40, 2, True)
    guard = panel_rounds.guard_churn_state(referee, 250, False)
    assert got["unrefereed"] == guard["lines"] == referee["unrefereed"] == 380
    assert got["production"] == referee["production"] == 177


def test_deletions_count_because_that_is_the_unit_the_budget_is_SPENT_in():
    """Inherited from `referee_split`, and asserted here because the budget is the
    reason that reader counts churn rather than additions: `git diff --numstat` reports
    insertions plus deletions, and that is what the fixer is told to count."""
    diff = ("diff --git a/app/a.py b/app/a.py\n--- a/app/a.py\n+++ b/app/a.py\n"
            "@@ -1,2 +1,2 @@\n-old\n+new\n")
    got = panel_rounds.fix_budget_state(
        panel_rounds.referee_state(panel_rounds.referee_split(diff), False),
        40, 2, True)
    assert got["spend"] == 2


@pytest.mark.parametrize("weight", [1, 2, 5])
def test_the_weight_in_force_is_the_weight_APPLIED(weight):
    """The dial the brief relays and the dial the payload prices with are one value
    read once in `panel.py`. A repo that repriced its guard work gets a spend repriced
    with it, or the two numbers a reader compares are measuring different things."""
    assert _spend(production=4, test=6, weight=weight)["spend"] == 4 + 6 * weight


# --------------------------------------------------------------------- one-sidedness

def test_a_pass_UNDER_the_budget_is_a_FACT_about_the_budgeted_part_of_it():
    """The verdict this whole block exists to publish, and the reason the upper bound
    is worth taking at all. The budget bounds the 💸 band, which is a SUBSET of the
    pass; so a whole pass priced under the budget puts the subset under it too,
    whatever the fixer counted and whether or not the fixer counted."""
    assert _spend(production=10, test=5, limit=40, weight=2)["within"] is True


def test_a_pass_OVER_it_is_the_absence_of_that_fact_and_NOT_a_breach():
    """**The assertion most at risk of being lost by someone tightening this up.** A
    round clearing two P1s may spend three hundred production lines the budget never
    applied to, and a diff cannot attribute a line to the finding it paid for. So
    `false` means "this round cannot show the budget was kept", and a consumer reading
    it as an accusation is wrong about a healthy round.

    Pinned in the prose as well as in the field, because the field cannot carry the
    caveat and the sentence a human reads is where the mistake actually gets made."""
    assert _spend(production=300, test=5, limit=40)["within"] is False
    text = panel_rounds.fix_budget_state.__doc__
    assert "Not a breach" in text
    assert "must not read it as an accusation" in text


def test_the_budget_is_crossed_STRICTLY():
    """A pass exactly at the budget is inside it, the reading every other ceiling in
    this file takes."""
    assert _spend(production=40, limit=40)["within"] is True
    assert _spend(production=41, limit=40)["within"] is False


def test_the_stricter_form_is_RECORDED_as_deferred_rather_than_silently_absent():
    """Where a round's whole To fix list is budgeted, every line of the pass answering
    it IS budget spend and `spend > limit` is a breach outright. That needs the PRIOR
    round's list and the dials it was banded under, read back out of a baseline payload
    — a verdict at the mercy of a payload written under different dials by a different
    version. #622 asks for the cheap half first; the docstring says which half this is,
    so the next reader does not have to rediscover that the strict form was considered.
    """
    text = panel_rounds.fix_budget_state.__doc__
    assert "considered and left out" in text
    assert "entire To fix list is budgeted" in text


# ------------------------------------------------------------- nothing to measure

def test_a_round_with_no_pass_to_read_measures_NOTHING_and_never_a_ZERO():
    """Round 1, and a round whose fix range could not be read. A published `0` would
    say a fix pass spent nothing when what happened is that there was no fix pass —
    and "spent nothing" is the flattering direction on exactly the claim this block
    exists to make. `guard_churn_state`'s argument, which a codex second opinion had
    to point out there and which is not going to be relearned here."""
    got = _spend(measured=False)
    assert got["spend"] is None and got["production"] is None
    assert got["unrefereed"] is None and got["within"] is None


def test_a_pass_that_genuinely_churned_nothing_is_the_same_answer_here():
    """The one accepted conflation, `churn_cells`' own: a pass that wrote nothing and a
    round that read no range record zeros in every bucket and the payload distinguishes
    them nowhere else. Kept rather than fixed, because the alternative is publishing a
    `0` for round 1."""
    assert _spend(production=0, test=0, prose=0)["spend"] is None


def test_no_budget_written_is_no_verdict_however_big_the_pass():
    """`low_severity_fix_lines: null` — the round's fix floor is then unconditional and
    there is nothing being paid for out of a budget. `limit: null` is the field a
    consumer reads to tell "inside the budget" from "there was no budget"."""
    got = _spend(production=9_999, limit=None)
    assert got["limit"] is None and got["within"] is None
    assert got["spend"] == 9_999


def test_a_budget_written_but_INERT_is_the_same_answer_and_says_which_it_was():
    """`Dials.budgeted_band`: at `fix_severity_floor: P2` with the default trigger floor
    the two meet, nothing sits between them, and the budget pays for nothing. The
    number must not be published as a limit in force — a reader comparing a spend
    against it would be comparing it to a budget no finding could draw on — and `band`
    is beside it for the reader who needs to know which of the two cases it was."""
    got = _spend(production=500, limit=40, band=False)
    assert got["limit"] is None and got["within"] is None and got["band"] is False
    assert _spend(production=500, limit=40, band=True)["band"] is True


# ------------------------------------------------------------------ it decides nothing

def test_it_gates_NOTHING_in_either_direction():
    """#67's rule, enforced rather than documented. The same round with a pass ten
    times over its budget and with one inside it has to reach the same verdict: nothing
    in `round_stop` reads this to move `stop`, `confident`, `converged` or the veto
    list, and it files no reason of its own."""
    over = panel.round_stop(2, 5, [], [], [], fix_budget=_spend(production=4_000))
    under = panel.round_stop(2, 5, [], [], [], fix_budget=_spend(production=4))
    assert over["stop"] == under["stop"] is True
    assert over["confident"] == under["confident"] is True
    assert over["converged"] == under["converged"] is True
    assert over["veto"] == under["veto"] == []
    assert over["reason"] == under["reason"]
    assert "budget" not in over["reason"]


def test_there_is_no_FLAG_to_arm_one_either():
    """#621 is explicit that this epic is not a 29th dial: every item makes an existing
    decision durable or moves an existing rule from prose into code. `guard_churn` ships
    with `escalate_on.guard_lines` one flag away from stopping a cycle; this ships with
    nothing, and adding a key here would be the thing the epic refuses."""
    assert "budget" not in json.dumps(DEFAULT_BLOCK["escalate_on"])
    # Every dial whose name mentions a budget, so a key added for this would show up
    # here rather than in whatever this list happens to contain. The `budget.*` block
    # is #191's spend ceiling and `budget_window_hours` is its window — neither is
    # `low_severity_fix_lines` and none of them is new.
    assert {k for k in harness_rules.BOARD_DIALS if "budget" in k} == {
        "review_panel.budget.tokens_per_day", "review_panel.budget.runs_per_day",
        "review_panel.budget.tokens_per_pr", "review_panel.budget.runs_per_pr",
        "review_panel.budget.fleet_tokens_per_day",
        "review_panel.budget_window_hours"}


def test_the_block_is_ALWAYS_present_even_where_there_was_nothing_to_measure():
    """Its siblings' rule: a payload with no key and a round with no fix pass to read
    are different claims, and a consumer forced to tell them apart would be reading the
    payload's age rather than the cycle's state. It carries no `fired` field, on
    `fix_surface`'s terms — there is no verdict to have."""
    d = panel.round_stop(1, 5, [], [], [])
    assert d["fix_budget"]["within"] is None and d["fix_budget"]["spend"] is None
    assert "fired" not in d["fix_budget"]


def test_the_measurement_is_published_whole_and_unchanged():
    """`round_stop` normalises rather than recomputes, so the payload and the printed
    line are one measurement rather than two derivations that can disagree."""
    got = _spend(production=10, test=5)
    assert panel.round_stop(2, 5, [], [], [], fix_budget=got)["fix_budget"] == got


# ----------------------------------------------------------- through a whole round

CFG = {"github": "acme/board", "path": "/nonexistent/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}


def _compare(added):
    """A fix range of `added` churned production lines, in the shape
    `_fix_range_diff` reads. `ahead` is the linear case — a branch that only grew
    between rounds — which is the only one that HAS a fix range to attribute."""
    body = "".join(f"+line{i}\n" for i in range(added))
    return json.dumps({"status": "ahead",
                       "files": [{"filename": "app/sync.py",
                                  "patch": f"@@ -1,0 +1,{added} @@\n{body}"}]})


def _round(monkeypatch, capsys, tmp_path, *, round_no=1, baseline=(), added=1,
           panel_block=None):
    """One panel run whose fix range churned `added` production lines. Round 2 with a
    baseline is what makes the round attributable, which is the condition the
    measurement is taken under. The head moves per round because an unchanged head is
    "no commit landed between rounds" — a range that does not exist rather than one
    that spent nothing."""
    # #551's proportional half off unless a caller says otherwise. This file is about
    # #622's READER — the arithmetic and the one-sidedness of its verdict — and the
    # fixture PR is ~120 chars, on which the pro-rata share of round 1 legitimately cuts
    # the budget to its clamp. Left on, every assertion here would be about the budget's
    # SIZE rather than about who counted the pass. The cut itself is pinned in
    # `test_panel_dials` section 8b, including its arrival in this same payload block.
    cfg = {**CFG,
           "review_panel": {"low_severity_fix_full_chars": None,
                            **(panel_block or {})}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "h", "headRefOid": f"{round_no:040d}",
              "files": [{"path": "app/sync.py", "additions": 3, "deletions": 1}]},
        diff=("diff --git a/app/sync.py b/app/sync.py\n--- a/app/sync.py\n"
              "+++ b/app/sync.py\n@@ -1,0 +1,1 @@\n+line\n"),
        compare=_compare(added)))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=5,
                     scope="pr") == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def test_round_one_prices_nothing_and_the_report_says_nothing(
        monkeypatch, capsys, tmp_path):
    """There is no pass before it, so there is no spend — and the report stays quiet
    rather than printing a line reading "0 lines against a 40-line budget"."""
    report, payload, _ = _round(monkeypatch, capsys, tmp_path)
    assert payload["round_stop"]["fix_budget"]["spend"] is None
    assert "Budget spend of the last fix pass" not in report


def test_a_pass_inside_the_budget_is_priced_from_OUTSIDE_it_and_printed(
        monkeypatch, capsys, tmp_path):
    """End to end, because the point of the whole change is that the number reaches the
    payload without passing through the agent it constrains."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path)
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[r1], added=9)
    got = payload["round_stop"]["fix_budget"]
    assert got["spend"] == 9 and got["limit"] == 40 and got["within"] is True
    assert "**Budget spend of the last fix pass:**" in report
    assert "the WHOLE pass fits inside it" in report
    assert "Counted here rather than by the fixer (#622)." in report


def test_a_pass_over_the_budget_says_it_could_not_be_SHOWN_rather_than_accusing(
        monkeypatch, capsys, tmp_path):
    """The sentence a human reads on the loud round, and the one that has to stay
    careful: the budget bounds the 💸 band, so a big pass is unproven and not guilty."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path)
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[r1], added=850)
    assert payload["round_stop"]["fix_budget"]["within"] is False
    assert "That is not a breach" in report
    assert "cannot show the budget was kept" in report
    assert "nothing stops on this (#67)." in report


def test_a_repo_with_no_budget_gets_no_line_at_all(
        monkeypatch, capsys, tmp_path):
    """A line reading "850 lines, no budget" on every round of every such repo is the
    loud-and-wrong a reader learns to skip. The count is in `round_stop.fix_budget`
    either way."""
    block = {"low_severity_fix_lines": None}
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path, panel_block=block)
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[r1], added=850, panel_block=block)
    got = payload["round_stop"]["fix_budget"]
    assert got["spend"] == 850 and got["limit"] is None and got["within"] is None
    assert "Budget spend of the last fix pass" not in report


def test_the_fixers_brief_says_the_count_no_longer_rests_on_its_arithmetic(
        monkeypatch, capsys, tmp_path):
    """Stated to the fixer rather than left in the payload. A measurement the measured
    party does not know about is a trap, and the point of this is a brake rather than a
    gotcha — the round prices the pass whatever the fixer's own arithmetic said, so the
    fixer may as well be told.

    Rendered through `test_panel_dials`' own band fixture rather than read off the
    source, because the sentence has to survive into the note an orchestrator actually
    pastes: it is one clause inside the 💸 paragraph, and a paragraph that stopped being
    emitted would leave a source assertion passing."""
    report, _, _ = band_run(monkeypatch, capsys, tmp_path, [finding("P3")])
    assert "The next round prices this pass itself" in report
    assert "publishes it at `round_stop.fix_budget`" in report
    assert "so the count does not rest on your arithmetic (#622)" in report
