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

Four things are pinned here:

* the MEASUREMENT — the pass priced exactly as the brief prices it, off the same split
  `unrefereed_fix` and `guard_churn` read, so a repo's budget and its ceiling can never
  count different lines;
* the ONE-SIDEDNESS of `within` — the budget bounds the 💸 band and not the pass, and a
  diff cannot say which line paid for which finding. `within: true` is a fact about the
  pass; `within: false` is the ABSENCE of that fact and must never render as a breach.
  This is the assertion most likely to be lost by someone tightening the wording, and it
  survived the strict half being built on top of it;
* the PREMISE that makes a strict verdict possible — where every finding the last round
  sent the fixer to answer was budgeted, there was no mandatory work for the spend to
  belong to and `spend > limit` is a fact. `breach` is that verdict, it is `null`
  wherever the premise could not be established, and the refusals matter more than the
  verdict: the failure this can cause is not a missed breach but an accusation against a
  round that did not commit one;
* the SILENCE that remains — the upper bound still gates nothing, and nothing here arms
  anything or adds a dial. #67 for the first, and #621's "not a 29th dial" for the rest.
"""

import ast
import inspect
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import panel  # noqa: E402
import panel_caps  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
import panel_rounds  # noqa: E402
import panel_seats  # noqa: E402
from conftest import gh_stub  # noqa: E402

# The band fixture is `test_panel_dials`' — two floors a tier apart, so there IS a
# budgeted band and the 💸 note renders. Imported rather than rebuilt: the sentence this
# file asserts on lives inside that note, and a second fixture drifting from the first
# would let the note change shape with this test still green.
from test_panel_dials import _adjudicate, band_run, finding

DEFAULT_BLOCK = harness_rules.DEFAULTS["review_panel"]


def _split(production=0, test=0, prose=0):
    """One fix pass as `referee_state` reads it, which is what the budget reads."""
    return panel_rounds.referee_state(
        {"production": production, "test": test, "prose": prose}, False)


def _spend(production=0, test=0, prose=0, limit=40, weight=2, band=True,
           measured=True, brief=None):
    """One pass priced against a budget, as `round_stop` receives it."""
    return panel_rounds.fix_budget_state(
        _split(production, test, prose) if measured else None,
        limit, weight, band, brief)


def _dials(floor="P4", cut="P2", limit=40, weight=2):
    """An anchor round's `review_panel` block as its payload carries it — the four keys
    `budgeted_brief` reads, spelled the way `Dials.as_dict` spells them."""
    return {"fix_severity_floor": floor, "round_trigger_floor": cut,
            "low_severity_fix_lines": limit, "unrefereed_line_weight": weight}


def _applied(limit=40, weight=2, band=True):
    """An anchor round's `round_stop.fix_budget` as its payload carries it, cut down to
    the two keys `budgeted_brief` reads. Since #551 this is where the budget that round
    actually SPENT against lives — `min(dial, its pro-rata share of round 1)`, clamped —
    and the dials block beside it carries only the written value."""
    return {"limit": limit if band else None, "weight": weight, "band": band}


def _prior(severities=("P3", "P4"), at=1, dials=None, applied=None):
    """The `Baseline` a round 2 holds: what round 1 asked its fixer to fix, the dials it
    banded that list under, and the budget it spent against. Built as the object
    `load_baseline` returns rather than as a payload, so the premise can be driven
    finding by finding — the end-to-end tests at the bottom of this file are what pin
    that the reader fills it."""
    return panel_rounds.Baseline(fixed_severities=list(severities), head_round=at,
                                 fixed_dials=_dials() if dials is None else dials,
                                 fixed_budget=(_applied() if applied is None
                                               else applied))


def _brief(severities=("P3", "P4"), at=1, round_no=2, dials=None, limit=40, weight=2,
           applied=None):
    """That baseline's premise, as `panel.py` computes it before pricing. `limit` is
    THIS round's applied budget (`Dials.budget_for`), which is what the anchor's own
    applied budget has to match."""
    return panel_rounds.budgeted_brief(_prior(severities, at, dials, applied), round_no,
                                       limit, weight)


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


# ------------------------------------------------------------------ the strict half
#
# What PR #668 deferred and this section pins. `within` alone is ONE-SIDED: `true` is a
# fact and `false` is the absence of one, so there was no input at all for which this
# reported a violation. The reviewer's case on #622 — a pass whose whole To fix list is
# budgeted P3/P4 findings, 41 production lines against `limit: 40` — is an unambiguous
# overspend, and the round used to proceed exactly as it does for a four-line pass.
#
# The premise that removes the need for per-line attribution is that the fixer had NO
# mandatory work in front of it. Everything below is either that premise being
# established, or it being refused — and the refusals are the important half, because
# the failure this feature can cause is not a missed breach (that is where the loop
# already was) but an accusation against a round that did not commit one.


def test_a_pass_over_its_budget_with_a_WHOLLY_BUDGETED_brief_is_a_BREACH():
    """#622's reviewer case, and the input that had no verdict before. Every finding
    round 1 sent the fixer to was in the 💸 band, so there was no mandatory work for
    the spend to belong to: the priced total is not an upper bound on the budgeted
    spend, it IS the budgeted spend, and 41 against 40 is a fact."""
    got = _spend(production=41, limit=40, brief=_brief(("P3", "P4", "P3")))
    assert got["within"] is False and got["breach"] is True
    assert got["brief"]["findings"] == 3 and got["brief"]["budgeted"] == 3
    assert got["brief"]["all_budgeted"] is True and got["brief"]["why"] is None


def test_the_same_premise_makes_a_pass_INSIDE_the_budget_a_stronger_claim():
    """`breach: False` is not `within: True` said twice. This one says the budget was
    kept by a pass that had no mandatory work to hide behind — the only round of which
    "the fixer stayed inside its budget" is a complete sentence."""
    got = _spend(production=39, limit=40, brief=_brief(("P3", "P4")))
    assert got["within"] is True and got["breach"] is False


def test_ONE_MANDATORY_FINDING_in_the_brief_and_there_is_no_verdict():
    """The whole of the one-sidedness, and it survives. A round that cleared a P2 may
    spend three hundred production lines the budget never applied to, and a diff cannot
    say it did not — so a single unbudgeted finding in the list takes the strict reading
    away and leaves exactly the unproven crossing that was there before."""
    got = _spend(production=850, limit=40, brief=_brief(("P3", "P2", "P4")))
    assert got["within"] is False and got["breach"] is None
    assert got["brief"]["budgeted"] == 2 and got["brief"]["all_budgeted"] is False
    assert "mandatory work" in got["brief"]["why"]


def test_an_UNREADABLE_severity_declines_the_brief_rather_than_being_guessed_at():
    """`Baseline` records `"?"` for a finding whose band nothing could parse, and it
    counts it — dropping it would turn a mixed list into an all-budgeted one and
    manufacture the premise for an accusation. The refusal comes out of
    `severity_at_least`'s own fallback (an unreadable severity reads as P1, and P1 is
    never budgeted) rather than out of a second rule written to agree with it."""
    assert _spend(production=99, limit=40,
                  brief=_brief(("P3", "?")))["breach"] is None


def test_an_EMPTY_brief_is_refused_by_name_rather_than_being_vacuously_true():
    """`all()` over an empty list is true, which is the cheapest way there is to
    manufacture this premise. A round that asked for nothing and got 900 churned lines
    is a real thing to worry about and it is #619's question about surface, not this
    one's about budget."""
    got = _spend(production=900, limit=40, brief=_brief(()))
    assert got["breach"] is None and got["brief"]["all_budgeted"] is False
    # `findings` is null and not `0`, and `why` is what tells this apart from round 1:
    # "the prior round asked for nothing" and "there was no prior round" are different
    # claims about the cycle, and a `0` for either is the second in the first's clothes.
    assert got["brief"]["findings"] is None
    assert got["brief"]["why"] == ("round 1 asked its fixer to fix nothing, so there "
                                   "is no brief for this pass to have spent against")


def test_a_ROUND_1_pass_has_no_brief_to_test_and_reaches_no_verdict():
    """Nothing preceded it. `brief` is present and null-filled rather than absent, its
    siblings' rule: a payload with no key and a round with no brief are different
    claims."""
    got = _spend(production=900, limit=40)
    assert got["breach"] is None
    assert got["brief"]["why"] == "no earlier round's To fix list reached this round"


def test_an_ANCHOR_THAT_IS_NOT_THE_ROUND_BEFORE_reaches_no_verdict():
    """`head_round` is the latest EARLIER round that recorded a commit, not necessarily
    the previous one: on a cycle whose round 2 payload named no head, round 3's fix
    range spans two fix passes while the brief describes one. `revert_state` keeps the
    same case apart as `spans` and prints a caveat; there is no caveat that makes an
    accusation safe, so this is refused."""
    got = _spend(production=99, limit=40, brief=_brief(("P3",), at=1, round_no=3))
    assert got["breach"] is None and got["brief"]["round"] == 1
    assert "spans more than round 2's To fix list" in got["brief"]["why"]


@pytest.mark.parametrize("dials,gist", [
    ({**_dials(), "fix_severity_floor": "nonsense"}, "band floors"),
    ({**_dials(), "round_trigger_floor": None}, "band floors"),
    ({k: v for k, v in _dials().items() if k != "fix_severity_floor"}, "band floors"),
    ({**_dials(), "low_severity_fix_lines": True}, "as a number"),
    ({**_dials(), "low_severity_fix_lines": "40"}, "as a number"),
])
def test_a_prior_payload_whose_BAND_DIALS_are_unreadable_reaches_no_verdict(dials,
                                                                           gist):
    """The `review_panel` block decides ONE thing for this premise since #551 — which
    findings were in the band at all — and these are the ways it can fail to.

    The floors are validated rather than left to `severity_at_least`'s fallbacks
    because those are not symmetric: an unreadable TRIGGER floor budgets nothing
    (harmless) and an unreadable FIX floor admits EVERYTHING to the band, which is the
    loosening direction on the one test that must not loosen. `low_severity_fix_lines`
    is validated for its own version of the same hazard: `Dials.budgeted` reads it for
    truthiness, and `True` is an `int` in Python while `"40"` is a truthy string, so
    either would put findings in a band on a value that says nothing about one."""
    got = _spend(production=99, limit=40, brief=_brief(("P3",), dials=dials))
    assert got["breach"] is None and gist in got["brief"]["why"]


@pytest.mark.parametrize("applied", [
    _applied(limit=13),
    _applied(weight=1),
    _applied(band=False),
    {},
    {"limit": 40},
    {"limit": True, "weight": 2},
])
def test_a_round_that_SPENT_against_a_different_budget_reaches_no_verdict(applied):
    """The check that moved when #551 landed, and the one that matters most on a small
    PR. The published `spend` is priced with this round's weight and compared against
    this round's applied budget, so a breach is only about the fixer's own bound if the
    anchor round applied the same one — otherwise the accusation is about a policy
    nobody ran.

    `{}` is a payload written before #622 recorded a budget block at all, and
    `{"limit": 40}` one that recorded half of it: neither can show what that round
    spent against, so neither establishes the premise."""
    got = _spend(production=99, limit=40, brief=_brief(("P3",), applied=applied))
    assert got["breach"] is None
    assert "spent against an applied budget" in got["brief"]["why"]


def test_the_budget_compared_is_the_one_APPLIED_and_not_the_one_WRITTEN():
    """**The behaviour change #551 forced, and the false breach it prevents.**

    Since #551 the number in the fixer's brief is `min(low_severity_fix_lines, its
    pro-rata share of the cycle's first round)`, clamped. Two rounds of one cycle can
    apply different budgets with nobody touching a dial — round 1 takes that
    denominator from `len(review.diff)` and round 2 takes round 1's recorded
    `pr_chars`, which are two readings of one size. Checked on the DIAL, a pair of
    rounds that spent against 13 and 15 lines would both read `40` and pass, and the
    pass would be priced against a bound it was never given.

    Both halves are asserted, because only the pair says which number is being read:
    a written dial that differs is fine while the applied budgets agree, and applied
    budgets that differ decline however identical the dials are."""
    # Same applied budget, different written dial: the dial decides band membership and
    # a P3 is in the band at 40 or 80, so the premise holds.
    loose = _brief(("P3",), dials=_dials(limit=80), applied=_applied(limit=40))
    assert loose["all_budgeted"] is True and loose["why"] is None
    # Same written dial, different applied budget: #551's case, and it declines.
    tight = _brief(("P3",), dials=_dials(limit=40), applied=_applied(limit=13))
    assert tight["all_budgeted"] is False
    assert "an applied budget of 13" in tight["why"] and "against 40" in tight["why"]
    assert _spend(production=41, limit=40, brief=tight)["breach"] is None


def test_the_floors_that_band_the_list_are_the_ANCHOR_ROUNDS_and_not_this_ones():
    """The fixer was briefed under that policy and spent under it, so it is the policy
    it is fairly measured against. An operator who dropped `fix_severity_floor` between
    rounds must not thereby reclassify what the last pass was paying for — here round
    1 ran at `fix_severity_floor: P3`, so its P4 was BELOW the floor and never the
    fixer's work at all, and the brief is not wholly budgeted whatever this round's
    floors say."""
    got = _spend(production=99, limit=40,
                 brief=_brief(("P3", "P4"), dials=_dials(floor="P3")))
    assert got["brief"]["budgeted"] == 1 and got["breach"] is None


def test_a_budget_of_ZERO_leaves_nothing_for_the_band_to_be_paid_out_of():
    """`Dials.budgeted` is false at a budget of `0` — the band is then below
    `fix_floor` and its findings render as below-floor ones — so no brief is wholly
    budgeted there and the strict reading is unavailable, which is right: at zero the
    applied fix floor IS the cut and every finding the fixer was sent to was mandatory.
    One predicate, read once, rather than a second rule here that has to agree."""
    got = _spend(production=99, limit=0,
                 brief=_brief(("P3",), dials=_dials(limit=0), limit=0,
                              applied=_applied(limit=0)))
    assert got["brief"]["budgeted"] == 0 and got["breach"] is None


def test_the_premise_is_decided_by_ONE_predicate_and_it_is_the_ROUNDS_OWN():
    """`Dials.budgeted` is what marks a finding 💸 in the fixer's list and what the
    report reads. A second spelling in `budgeted_brief` would let a finding be listed
    as budgeted and measured as mandatory in the same cycle, which is the duplicated-
    measurement failure this file keeps writing down."""
    was = panel_seats.Dials(fix_severity_floor="P4", round_trigger_floor="P2",
                            low_severity_fix_lines=40)
    for sev in ("P1", "P2", "P3", "P4"):
        got = panel_rounds.budgeted_brief(_prior((sev,)), 2, 40, 2)
        assert got["all_budgeted"] is was.budgeted(sev)


def test_nothing_here_COUNTS_A_LINE():
    """The premise is a premise. `budgeted_brief` decides whether the strict reading is
    available and the arithmetic stays where it was — one pricing, in one place — so
    the two numbers a reader compares cannot come from two derivations.

    Asserted over the parsed NAMES rather than over the source text. The docstring, the
    comments and one `why` sentence all discuss spend, as they should, and a grep for
    the word would either fail on that prose or be weakened until it caught nothing —
    which is the failure mode a grep-shaped assertion has every time. What the function
    must not do is READ the split, and the identifiers it mentions are exactly that."""
    fn = ast.parse(inspect.getsource(panel_rounds.budgeted_brief)).body[0]
    used = ({n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            | {n.value for n in ast.walk(fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)})
    assert not (used & {"referee", "spend", "churn", "production", "unrefereed",
                        "fix_budget_state", "referee_state", "referee_split"})


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


# ------------------------------------------------------- what it decides, and what not
#
# TWO verdicts about one number, and only one of them can end anything. The upper bound
# (`within`) is reported and gates nothing in either direction, exactly as it did — it
# is true of any round that cleared two P1s with three hundred production lines, and a
# cycle ended on it would be a machine calling an honest fix pass a spendthrift. The
# strict verdict (`breach`) fires on a proof and on nothing else, and it takes the same
# two bounds every other rung on that chain takes.


def _new(n=4):
    return [f"k{i}" for i in range(n)]


def test_an_UNPROVEN_crossing_still_gates_NOTHING_in_either_direction():
    """#67's rule, and the assertion most at risk of being lost by someone tightening
    this up. The same round with a pass ten times over its budget and one inside it has
    to reach the same verdict as long as the premise was not established: nothing reads
    `within` to move `stop`, `confident`, `converged` or the veto list."""
    over = panel.round_stop(2, 5, _new(), [], [], fix_budget=_spend(production=4_000))
    under = panel.round_stop(2, 5, _new(), [], [], fix_budget=_spend(production=4))
    assert over["stop"] == under["stop"] is False
    assert over["veto"] == under["veto"] == []
    assert over["reason"] == under["reason"]
    assert "budget" not in over["reason"]
    assert over["fix_budget"]["fired"] is under["fix_budget"]["fired"] is False


def test_there_is_no_FLAG_to_arm_one_either():
    """#621 is explicit that this epic is not a 29th dial: every item makes an existing
    decision durable or moves an existing rule from prose into code. `guard_churn` ships
    with `escalate_on.guard_lines` one flag away from stopping a cycle; this ships with
    nothing, and adding a key here would be the thing the epic refuses.

    The strict half did not change that and could not: the limit is
    `low_severity_fix_lines`, which the repo wrote; the band is the one its two floors
    already carve out; and the premise is proved rather than chosen. An `escalate_on`
    key would not be offering a choice, it would be offering to ignore an arithmetic
    breach of the repo's own policy."""
    assert "budget" not in json.dumps(DEFAULT_BLOCK["escalate_on"])
    # Every dial whose name mentions a budget, so a key added for this would show up
    # here rather than in whatever this list happens to contain. The `budget.*` block
    # is #191's spend ceiling and `budget_window_hours` is its window — neither is
    # `low_severity_fix_lines`, and the one addition since is `tokens_per_round`
    # (#483), which is that same spend ceiling denominated in the unit its work is
    # dispatched in. This list is a canary and not a ledger: a member added here has
    # to be a TOKEN-and-RUN ceiling measured against `GET /review/spend`, which is
    # what makes it not a dial for the churned-line budget above.
    assert {k for k in harness_rules.BOARD_DIALS if "budget" in k} == {
        "review_panel.budget.tokens_per_day", "review_panel.budget.runs_per_day",
        "review_panel.budget.tokens_per_round",
        "review_panel.budget.tokens_per_pr", "review_panel.budget.runs_per_pr",
        "review_panel.budget.fleet_tokens_per_day",
        "review_panel.budget_window_hours"}
    # And the canary's real claim, stated so it cannot be satisfied by editing a
    # literal: every one of them is a ceiling `panel_caps` measures, so none of them
    # is a dial for the fix budget whatever it is called.
    assert {k.rsplit(".", 1)[-1] for k in harness_rules.BOARD_DIALS
            if k.startswith("review_panel.budget.")} == set(panel_caps.CEILINGS)


def test_a_PROVEN_breach_ends_the_round_and_names_the_premise():
    """The input that had no verdict at all before this: 41 priced lines against a
    40-line budget, on a round whose whole To fix list was budgeted. It ends the cycle,
    and the reason says WHY the number is binding rather than citing an issue at a
    reader who then has to take the accusation on trust."""
    got = panel.round_stop(2, 5, _new(), [], [],
                           fix_budget=_spend(production=41, limit=40,
                                             brief=_brief(("P3", "P4", "P3"))))
    assert got["stop"] is True and got["fix_budget"]["fired"] is True
    why = got["reason"]
    assert "41 line(s) against the 40-line `low_severity_fix_lines` budget" in why
    assert "every one of the 3 finding(s) it was sent to answer was budgeted" in why
    assert "a human decides" in why.lower()


def test_that_stop_is_never_reported_as_convergence():
    """The discipline every stop in this file gets: a veto line naming what happened and
    `confident` false. This one has to say out loud that it is a BREACH and not the
    ordinary unproven crossing, because a reader skimming the list will otherwise supply
    the other budget sentence they have seen — the report's line under `within: false`,
    which says nearly the opposite in nearly the same words."""
    got = panel.round_stop(2, 5, _new(), [], [],
                           fix_budget=_spend(production=41, limit=40,
                                             brief=_brief(("P3", "P4"))))
    assert got["confident"] is False and got["converged"] is False
    line = next(v for v in got["veto"] if "#622" in v)
    assert "That is a BREACH of a number this repo wrote" in line
    assert "not the ordinary `within: false`" in line
    assert "no mandatory work for that spend to belong to" in line
    assert "not convergence (#622)" in line


def test_it_can_only_turn_a_GO_AGAIN_into_a_stop():
    """`not stop` is a condition rather than a redundancy, its siblings' rule: a dry
    round, a below-floor policy stop and a round holding an escalation each keep the
    reason and the confidence they earned, however far over its budget the pass went."""
    got = panel.round_stop(2, 5, [], [], [],
                           fix_budget=_spend(production=41, limit=40,
                                             brief=_brief(("P3", "P4"))))
    assert got["stop"] is True and got["confident"] is True
    assert got["reason"].startswith("dry")
    assert got["fix_budget"]["fired"] is False


def test_it_may_not_cancel_the_repair_round_for_a_P1_AN_EARLIER_ROUND_RAISED():
    """The second bound, the one a codex second opinion found missing from #505's first
    draft. The argument for this rung is about rule 1 — new findings buy another round —
    so it may only take away the round rule 1 was buying. An overspend is a fact about
    the pass; it may not overrule a named P1 the pass did not clear."""
    held_over = panel.Canonical(id="x", severity="P1", file="app/a.py", line=1,
                                synthesis="a dangling handle", verdict="confirmed",
                                detail="d", reported_by=[], rationale="real")
    got = panel.round_stop(2, 5, _new(), [held_over], [], repeated={"x"},
                           fix_budget=_spend(production=41, limit=40,
                                             brief=_brief(("P3", "P4"))))
    assert got["stop"] is False and got["fix_budget"]["fired"] is False


def test_BREACH_and_FIRED_are_kept_apart_on_fix_injections_terms():
    """`breach` is a property of the MEASUREMENT and is true of every round that
    committed one, including the rounds this rung is deliberately bounded out of.
    `fired` is the property of the VERDICT. Collapsed into one field, a payload would
    record `false` for a pass that really did break its budget on every round the bound
    kept the rung off — which is the reporting failure `fix_injection` keeps `over` and
    `fired` apart to avoid."""
    got = panel.round_stop(2, 5, [], [], [],
                           fix_budget=_spend(production=41, limit=40,
                                             brief=_brief(("P3", "P4"))))
    assert got["fix_budget"]["breach"] is True and got["fix_budget"]["fired"] is False


def test_the_KIND_of_work_the_pass_did_is_the_more_specific_truth_than_its_COST():
    """The chain's ordering rule. #554 says the pass wrote no refereed line at all,
    which says more about it than "it went past a budget", so that rung owns the
    `reason` — and both veto lines stay on the record."""
    got = panel.round_stop(
        2, 5, _new(), [], [],
        unrefereed=panel_rounds.referee_state(
            {"production": 0, "test": 7, "prose": 3}, True),
        fix_budget=_spend(production=0, test=7, prose=3, limit=5,
                          brief=_brief(("P3", "P4"))))
    assert "not one of them was production code" in got["reason"]
    assert got["fix_budget"]["fired"] is True and got["unrefereed_fix"]["fired"] is True
    assert any("#554" in v for v in got["veto"]) and any("#622" in v for v in got["veto"])


def test_the_leftovers_of_a_breached_round_go_to_a_HUMAN_and_not_to_a_fix_pass():
    """#42. The rung's own `reason` ends "a human decides what of it was wanted, not
    another fix pass", so handing this round's remainder to one would contradict a
    sentence the same payload is carrying."""
    fixable = panel.Canonical(id="y", severity="P2", file="app/b.py", line=2,
                              synthesis="a race", verdict="confirmed", detail="d",
                              reported_by=[], rationale="real")
    # `fixable.key`, never its `id`: `held_over` is computed over `Canonical.key`, so a
    # test passing the id here would put this round's own finding in the held-over
    # bucket and bound the rung out — a round going again for a P1 nobody raised.
    got = panel.round_stop(2, 5, [fixable.key], [fixable], [],
                           fix_budget=_spend(production=41, limit=40,
                                             brief=_brief(("P3", "P4"))))
    assert got["fix_budget"]["fired"] is True
    assert got["outstanding"]["handed_to"] == "human"
    assert "contradict the reason above" in got["outstanding"]["why"]


def test_the_block_is_ALWAYS_present_even_where_there_was_nothing_to_measure():
    """Its siblings' rule: a payload with no key and a round with no fix pass to read
    are different claims, and a consumer forced to tell them apart would be reading the
    payload's age rather than the cycle's state. It carries no `fired` field, on
    `fix_surface`'s terms — there is no verdict to have."""
    d = panel.round_stop(1, 5, [], [], [])
    assert d["fix_budget"]["within"] is None and d["fix_budget"]["spend"] is None
    assert d["fix_budget"]["breach"] is None
    # `fired` IS here, unlike `fix_surface`'s block, and it arrived with the strict
    # half: there is a verdict to have now. Kept apart from `breach` on
    # `fix_injection`'s terms — `breach: true` is true of every round that committed
    # one, including the rounds the rung is deliberately bounded out of.
    assert d["fix_budget"]["fired"] is False


def test_the_measurement_is_published_whole_and_unchanged():
    """`round_stop` normalises rather than recomputes, so the payload and the printed
    line are one measurement rather than two derivations that can disagree. `fired` is
    the one field it adds, and it is a property of the VERDICT rather than of the
    measurement."""
    got = _spend(production=10, test=5)
    assert panel.round_stop(2, 5, [], [], [],
                            fix_budget=got)["fix_budget"] == {**got, "fired": False}


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
           panel_block=None, findings=()):
    """One panel run whose fix range churned `added` production lines. Round 2 with a
    baseline is what makes the round attributable, which is the condition the
    measurement is taken under. The head moves per round because an unchanged head is
    "no commit landed between rounds" — a range that does not exist rather than one
    that spent nothing.

    `findings` are what the seats report and the judge confirms, which is what puts a
    To fix list in the payload — the half the strict verdict reads back out of a
    baseline. Empty by default, because most of this file is about the measurement and
    not about the premise."""
    # #551's proportional half off unless a caller says otherwise. This file is about
    # #622's READER — the arithmetic and the one-sidedness of its verdict — and the
    # fixture PR is ~120 chars, on which the pro-rata share of round 1 legitimately cuts
    # the budget to its clamp. Left on, every assertion here would be about the budget's
    # SIZE rather than about who counted the pass. The cut itself is pinned in
    # `test_panel_dials` section 8b, including its arrival in this same payload block.
    #
    # It matters twice over for the strict verdict: `budgeted_brief` refuses a premise
    # whose anchor round applied a different budget from this one's, and round 1 takes
    # its denominator from `len(review.diff)` while round 2 takes round 1's recorded
    # `pr_chars` — two readings that need not agree. Left on, these tests would be
    # exercising that refusal rather than the thing each of them names.
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
                        lambda *a, **k: panel.ReviewerRun(list(findings), None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _adjudicate)
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
    # The BUDGET line and not the whole report: three other features print "nothing
    # stops on this (#67)" in the same block, so a bare `in report` for that clause
    # passed against `fix_surface`'s sentence while this one said something else
    # entirely. Pulled out by its own heading instead.
    line = next(ln for ln in report.splitlines()
                if ln.startswith("**Budget spend of the last fix pass:**"))
    assert "That is not a breach" in line
    assert "cannot show the budget was kept" in line
    assert "an unproven crossing stops nothing (#67)." in line
    # Round 1 asked for nothing, which is not the same as no round 1 — and the report
    # says which, because "why is there no strict verdict here" has five answers.
    assert "round 1 asked its fixer to fix nothing" in line


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


def test_a_PROVABLE_overspend_reaches_the_payload_the_veto_and_the_report(
        monkeypatch, capsys, tmp_path):
    """#622's reviewer case driven through two real rounds, which is the only way to
    pin the half that had to be built: the prior round's To fix list and the dials it
    was banded under, read back out of a baseline payload by `load_baseline` rather
    than handed to the reader by a test.

    Round 1 asks for a P3 and a P4 and nothing else. Round 2 measures 41 production
    lines against the 40-line budget both rounds ran under. There was no mandatory work
    for that spend to belong to, so the priced total IS the budgeted spend — and this
    is now a breach rather than the absence of an assurance."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path,
                            findings=[finding("P3", "a stale docstring"),
                                      finding("P4", "a typo", file="b.py")])
    assert [f["severity"] for f in first["to_fix"]] == ["P3", "P4"]
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[r1], added=41,
                                findings=[finding("P2", "a race", file="c.py")])
    got = payload["round_stop"]["fix_budget"]
    assert got["spend"] == 41 and got["limit"] == 40 and got["within"] is False
    assert got["breach"] is True and got["fired"] is True
    assert got["brief"] == {"round": 1, "findings": 2, "budgeted": 2,
                            "all_budgeted": True, "why": None}
    assert payload["round_stop"]["stop"] is True
    assert payload["round_stop"]["confident"] is False
    assert any("not convergence (#622)" in v for v in payload["round_stop"]["veto"])
    assert "**Budget spend of the last fix pass:** BROKE it" in report
    assert "There was no mandatory work for that spend to belong to" in report
    assert "that is what ended this cycle" in report


def test_ONE_P2_in_the_prior_list_and_the_same_pass_is_only_UNPROVEN(
        monkeypatch, capsys, tmp_path):
    """The identical fix range against the identical budget, one round earlier finding
    changed from P4 to P2. The pass may have spent every one of those lines on the P2,
    which the budget never applied to, and a diff cannot say it did not — so the round
    goes on exactly as it did before this feature, and the report says which of the two
    sentences it is saying and why."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path,
                             findings=[finding("P3", "a stale docstring"),
                                       finding("P2", "a race", file="b.py")])
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[r1], added=41,
                                findings=[finding("P2", "a race", file="c.py")])
    got = payload["round_stop"]["fix_budget"]
    assert got["spend"] == 41 and got["within"] is False and got["breach"] is None
    assert got["brief"]["budgeted"] == 1 and got["fired"] is False
    assert not any("#622" in v for v in payload["round_stop"]["veto"])
    assert "That is not a breach" in report
    assert "cannot show the budget was kept" in report
    assert "1 of round 1's 2 finding(s) were mandatory work" in report
    assert "an unproven crossing stops nothing (#67)." in report


def test_the_fixers_brief_says_what_a_WHOLLY_BUDGETED_list_costs(
        monkeypatch, capsys, tmp_path):
    """The other half of the same sentence, and it is there for the same reason: a
    fixer told about the measurement but not about the consequence has been told half of
    it, and the one reading here that ends a cycle is the half it was not told.

    Said with its precondition attached — "every finding in the list below" — because
    that is a fact about THIS brief which the fixer can check against the list directly
    beneath the note, rather than a rule it has to take on trust about a payload it
    never sees."""
    report, _, _ = band_run(monkeypatch, capsys, tmp_path, [finding("P3")])
    assert "Where every finding in the list below is 💸" in report
    assert "that price IS the budgeted spend" in report
    assert "ends the cycle for a human to look at" in report


# ---------------------------------------------- the premise, read off a real payload
#
# `budgeted_brief` is only as good as what `load_baseline` puts in front of it, and the
# two halves are testable apart — which is exactly how a reader that drops a finding
# ships green. These drive the real reader over a real payload.


def _payload(to_fix, *, round_no=1, dials=None, sonar=(), applied=None):
    """One earlier round's `--json-file`, cut down to the keys `load_baseline` reads."""
    return {"github": "acme/board", "pr": 34, "round": round_no, "cycle": "cyc",
            "reviewed": True, "scope": "pr", "head_sha": f"{round_no:040d}",
            "to_fix": to_fix, "sonar_findings": list(sonar), "dismissed": [],
            "review_panel": _dials() if dials is None else dials,
            "round_stop": {"fix_budget": _applied() if applied is None else applied}}


def _loaded(tmp_path, payload):
    path = tmp_path / f"r{payload['round']}.json"
    path.write_text(json.dumps(payload))
    return panel_rounds.load_baseline([str(path)],
                                      {"github": "acme/board", "pr": 34, "round": 2})


def test_an_UNPLACEABLE_finding_still_counts_against_the_premise(tmp_path):
    """`fixed_findings` and `fixed_here` DROP a record with no file or no key — a
    finding nothing can place is no evidence the fixer was working anywhere, which is
    right for recurrence and is exactly wrong here. The question this list answers is
    whether the WHOLE brief was budgeted, so a dropped P1 would turn a mixed list into
    an all-budgeted one and manufacture the premise for an accusation."""
    got = _loaded(tmp_path, _payload([
        {"severity": "P3", "file": "a.py", "key": "k1"},
        {"severity": "P1", "file": "", "key": ""},
    ]))
    assert got.fixed_severities == ["P3", "P1"]
    assert list(got.fixed_here) == ["a.py"], "the placement guard still applies there"
    assert panel_rounds.budgeted_brief(got, 2, 40, 2)["all_budgeted"] is False


def test_a_BELOW_FIX_FLOOR_finding_still_counts_against_the_premise(tmp_path):
    """#746 moved the below-floor filter to the population site, and this is the
    consumer that had to be checked before it could move. `fixed_findings` answers "what
    was the fixer sent to" and now drops those rows; this list answers "was ALL of the
    brief budgeted", where dropping an entry is the one thing that must never happen —
    so the two fields diverge here exactly as they already do on an unplaceable record.

    **The dials are a shape the producer can actually emit**, which is what makes this a
    regression test rather than an assertion about contradictory input. `below_floor` is
    `not severity_at_least(severity, dials.fix_floor)`, so a P1 flagged below the floor
    is impossible from any dials at all and a test written on one would prove only that
    junk is handled. At `fix_severity_floor: P3` under the `P2` trigger floor the P3 is
    in the fixer's list and the P4 is below the floor, which is the real thing.

    The sentence is asserted as well as the verdict, because the sentence is where the
    cost of keeping this list wide shows up: the P4 is reported as "mandatory work,
    which this spend may have gone on", about a finding the fixer was forbidden to
    touch. That is wrong in the DECLINING direction — `all_budgeted` false is the answer
    that refuses to price a breach — it is what this consumer did before #746, and it is
    recorded on `fixed_findings` rather than changed on this issue's evidence."""
    got = _loaded(tmp_path, _payload([
        {"severity": "P3", "file": "a.py", "key": "k1", "below_fix_floor": False},
        {"severity": "P4", "file": "b.py", "key": "k2", "below_fix_floor": True},
    ], dials=_dials(floor="P3")))
    assert got.fixed_severities == ["P3", "P4"]
    assert [k for k, *_ in got.fixed_findings] == ["k1"]
    assert list(got.fixed_here) == ["a.py"]
    # The flag on that payload is the one the producer would have written: at these
    # dials the applied floor is P3, so the P3 is briefed and the P4 is not.
    was = panel_seats.Dials(fix_severity_floor="P3", round_trigger_floor="P2",
                            low_severity_fix_lines=40)
    assert was.fix_floor == "P3" and was.budgeted("P3") and not was.budgeted("P4")
    brief = panel_rounds.budgeted_brief(got, 2, 40, 2)
    assert brief["findings"] == 2 and brief["budgeted"] == 1
    assert brief["all_budgeted"] is False
    assert brief["why"] == ("1 of round 1's 2 finding(s) were mandatory work, which "
                            "this spend may have gone on and a diff cannot say it did "
                            "not")


def test_a_MALFORMED_entry_DECLINES_the_brief_rather_than_vanishing_from_it(tmp_path):
    """The defect a different-vendor review found in PR #694's first cut, and it is the
    same shape as the guard one line below it.

    `load_baseline` skipped any bucket entry that was not a mapping, because there is
    nothing in a bare string to read a severity off. True about the severity and false
    about the FINDING: a brief of one P3 and one malformed record then read as wholly
    budgeted, and a pass spending 41 lines against a 40-line budget — possibly on
    whatever that record said — was reported as a proven overspend. An unreadable
    finding must DECLINE the brief, exactly as an unreadable severity already does, and
    through the same sentinel so that no branch has to know this case exists.

    Driven through `load_baseline` and then all the way to `breach`, because the bug was
    in the reader and a unit test over `budgeted_brief` alone would never have seen it.
    """
    got = _loaded(tmp_path, _payload([
        {"severity": "P3", "file": "a.py", "key": "k1"},
        "malformed",
    ]))
    assert got.fixed_severities == ["P3", "?"]
    # And the two readers that answer "where was the fixer working" still drop it: an
    # entry nothing can read is no evidence about that, and the asymmetry is the point.
    assert list(got.fixed_here) == ["a.py"] and len(got.fixed_findings) == 1
    brief = panel_rounds.budgeted_brief(got, 2, 40, 2)
    assert brief["findings"] == 2 and brief["budgeted"] == 1
    assert brief["all_budgeted"] is False
    assert "carry no severity this round can read" in brief["why"]
    assert "must decline the brief rather than disappear from it" in brief["why"]
    assert _spend(production=41, limit=40, brief=brief)["breach"] is None


def test_an_unreadable_entry_and_a_mandatory_one_are_DIFFERENT_news(tmp_path):
    """Both decline, and the sentence has to say which. "The pass had mandatory work in
    front of it" is a statement about severity; a malformed entry establishes no such
    thing, and reporting it that way would assert something nothing measured. The
    verdict is `Dials.budgeted`'s either way — this only chooses wording, so a
    disagreement here can change a sentence and can never change `all_budgeted`."""
    mandatory = _loaded(tmp_path, _payload([
        {"severity": "P3", "file": "a.py", "key": "k1"},
        {"severity": "P1", "file": "b.py", "key": "k2"},
    ]))
    why = panel_rounds.budgeted_brief(mandatory, 2, 40, 2)["why"]
    assert "were mandatory work" in why and "no severity this round can read" not in why
    both = _loaded(tmp_path, _payload([
        {"severity": "P3", "file": "a.py", "key": "k1"},
        {"severity": "P1", "file": "b.py", "key": "k2"},
        "malformed",
    ]))
    mixed = panel_rounds.budgeted_brief(both, 2, 40, 2)
    assert mixed["findings"] == 3 and mixed["budgeted"] == 1
    assert "1 of round 1's 3 finding(s) carry no severity" in mixed["why"]
    assert "(1 more were mandatory work)" in mixed["why"]


def test_a_brief_of_NOTHING_BUT_malformed_entries_is_not_an_empty_one(tmp_path):
    """The two refusals are next to each other and must not collapse. An empty brief is
    "the round asked for nothing"; a brief of unreadable entries is "the round asked for
    things nobody can identify", and the second has to keep its finding count or the
    payload under-states what the pass was sent to do."""
    got = _loaded(tmp_path, _payload(["a", "b"]))
    brief = panel_rounds.budgeted_brief(got, 2, 40, 2)
    assert got.fixed_severities == ["?", "?"] and brief["findings"] == 2
    assert brief["budgeted"] == 0 and brief["all_budgeted"] is False
    assert "asked its fixer to fix nothing" not in brief["why"]


def test_the_SONAR_bucket_is_in_the_brief_and_the_DISMISSED_one_is_not(tmp_path):
    """The two buckets a fixer's brief is built from, and the one it is not: a finding
    the master ruled not real was sent to nobody, so a pass cannot have spent on it.
    Read through `load_baseline` rather than asserted about it, because those three
    bucket names are the contract between the payload and this premise."""
    got = _loaded(tmp_path, _payload(
        [{"severity": "P3", "file": "a.py", "key": "k1"}],
        sonar=[{"severity": "P4", "file": "b.py", "key": "k2"}]))
    got.problems.clear()
    assert got.fixed_severities == ["P3", "P4"]
    with_dismissed = _payload([{"severity": "P3", "file": "a.py", "key": "k1"}])
    with_dismissed["dismissed"] = [{"severity": "P1", "file": "c.py", "key": "k3"}]
    assert _loaded(tmp_path, with_dismissed).fixed_severities == ["P3"]


def test_the_dials_come_off_the_ANCHOR_payload_and_are_carried_WHOLE(tmp_path):
    """Whatever reads a policy back out of a payload has to be able to say which values
    it applied, so the block travels whole rather than picked apart in the reader —
    `first_reviewed`'s rule. Its consumer validates every key it touches, because this
    mapping can have been written by an older release or by hand."""
    block = _dials(floor="P3", cut="P2", limit=25, weight=3)
    spent = _applied(limit=25, weight=3)
    got = _loaded(tmp_path, _payload([{"severity": "P3", "file": "a.py", "key": "k1"}],
                                     dials=block, applied=spent))
    assert got.fixed_dials == block
    # And the budget that round SPENT against, off `round_stop.fix_budget` and kept
    # apart from the dials block beside it: since #551 they are two numbers.
    assert got.fixed_budget == spent
    assert panel_rounds.budgeted_brief(got, 2, 25, 3)["all_budgeted"] is True
    # And the round it was banded under is the one this round has to be next to.
    assert got.head_round == 1


def test_a_payload_carrying_NO_review_panel_block_reaches_no_verdict(tmp_path):
    """A baseline written before the block existed, or a hand-written one. There is
    then nothing to say which findings that round banded, and the premise is refused
    rather than assumed from this round's dials."""
    payload = _payload([{"severity": "P3", "file": "a.py", "key": "k1"}])
    del payload["review_panel"]
    got = _loaded(tmp_path, payload)
    assert got.fixed_dials == {}
    assert panel_rounds.budgeted_brief(got, 2, 40, 2)["all_budgeted"] is False


def test_the_two_rounds_agree_on_the_budget_with_the_PROPORTIONAL_HALF_ON(
        monkeypatch, capsys, tmp_path):
    """#551 put a second input under the number this reader prices against, and the
    premise now REFUSES a pair of rounds that applied different budgets. That refusal is
    correct and it is also the way this whole feature could quietly die: if round 1 and
    round 2 of an ordinary cycle computed different budgets, no cycle would ever
    establish the premise and nothing in the rest of this file would notice, because
    every other end-to-end test here turns the proportional half off.

    They agree, and the reason is checkable rather than lucky: round 1's denominator is
    `len(review.diff)` and its payload records `pr_chars` as that same expression, which
    is what round 2 reads back through `Baseline.first_reviewed`. One expression, two
    rounds.

    Driven at the DEFAULT `low_severity_fix_full_chars` so the fixture's ~120-char PR
    lands on #551's clamp — the smallest budget it can produce, and the setting under
    which a 41-line pass is furthest over it."""
    block = {"low_severity_fix_full_chars":
             harness_rules.DEFAULTS["review_panel"]["low_severity_fix_full_chars"]}
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, panel_block=block,
                            findings=[finding("P3", "a stale docstring"),
                                      finding("P4", "a typo", file="b.py")])
    clamp = panel_seats.MIN_HONEST_FIX_CHURN * 2
    assert first["round_stop"]["fix_budget"]["limit"] == clamp, "the clamp, not 40"
    _report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                 baseline=[r1], added=41, panel_block=block,
                                 findings=[finding("P2", "a race", file="c.py")])
    got = payload["round_stop"]["fix_budget"]
    # The two rounds applied ONE budget, so the premise holds and the breach is about
    # the number round 1's fixer was actually given.
    assert got["limit"] == clamp and got["brief"]["all_budgeted"] is True
    assert got["breach"] is True and got["fired"] is True


def test_a_cycle_whose_ROUNDS_DISAGREE_on_the_budget_reaches_no_verdict(
        monkeypatch, capsys, tmp_path):
    """The other half, end to end: round 1 runs with the proportional half OFF and round
    2 with it ON, so they apply 40 and the clamp. The pass is identical to the one above
    and the round says nothing — pricing it against a bound its fixer was never given is
    the false breach this check exists to prevent, and #551's note puts that divergence
    on small PRs, which is where the priced total is closest to the limit."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path,
                            findings=[finding("P3", "a stale docstring"),
                                      finding("P4", "a typo", file="b.py")])
    assert first["round_stop"]["fix_budget"]["limit"] == 40
    block = {"low_severity_fix_full_chars":
             harness_rules.DEFAULTS["review_panel"]["low_severity_fix_full_chars"]}
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[r1], added=41, panel_block=block,
                                findings=[finding("P2", "a race", file="c.py")])
    got = payload["round_stop"]["fix_budget"]
    assert got["limit"] == panel_seats.MIN_HONEST_FIX_CHURN * 2
    assert got["breach"] is None and got["fired"] is False
    assert "spent against an applied budget of 40" in got["brief"]["why"]
    assert "That is not a breach" in report
