"""#626: one boolean for "was this a clean finish", so nobody has to assemble it.

The number this whole convergence epic is judged on is the share of cycles ending in
a confident dry round, and until now a reader had to derive that from five fields —
`stop`, `confident`, an empty `veto`, an empty `outstanding.fixable` and an empty
`escalated_outstanding`. Five joins to answer one question is five places to get it
wrong, and every one of those mistakes errs in the direction that flatters the loop.

So `converged` is computed once, in `round_stop`, FROM `confident` rather than beside
it — a capped stop and a vetoed stop are false here by construction, not by two
expressions agreeing — and then made stricter: nothing a fix pass could take, nothing
under the cleared floor either, and no escalation being held.

That last strictness is where the judgement is, and it is the reason a below-floor
policy stop is `confident: True` and `converged: False`. #165 argues at length that
such a stop is a legitimate configured convergence and keeps its confidence, and
nothing here revisits that; but its `reason` is "reported, not fixed here" rather than
"dry", and a metric that counted it would be counting a cycle that ended with real
findings unfixed by policy as a clean finish. A false negative costs such a round
nothing it had. A false positive is the one thing reading this field is supposed to
make impossible.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_rounds  # noqa: E402  — the state builders the futility rungs take


def _finding(severity="P2", key_from="boom", file="a.py", line=1, verdict="confirmed"):
    reported = [panel.Finding("claude", severity, file, line, key_from, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=line,
                           synthesis=key_from, verdict=verdict,
                           reported_by=reported)


def _counts(introduced=9, missed=1):
    return {"introduced": introduced, "missed": missed,
            "missed-unread": 0, "unknown": 0}


# -------------------------------------------------------------- the one true case

def test_a_dry_stop_with_nothing_left_is_the_only_thing_that_converges():
    """The case the field exists to count: the round stopped, it was confident, no
    veto line was taken, and there is nothing outstanding at any floor."""
    d = panel.round_stop(2, 5, [], [], [])
    assert d["stop"] is True and d["confident"] is True
    assert d["reason"].startswith("dry")
    assert d["converged"] is True


def test_a_round_that_is_going_again_has_not_converged():
    """`stop` is the first conjunct, through `confident`. A mid-cycle round has not
    finished anything, and a consumer counting converged cycles must not be handed a
    true from a round that is about to run another one."""
    d = panel.round_stop(2, 5, ["k1"], [_finding("P2")], [])
    assert d["stop"] is False
    assert d["converged"] is False


# ------------------------------------------------ the four stops that are not clean

def test_a_capped_stop_is_not_convergence():
    """"Nothing left to find" is a claim; "the counter hit zero" is not the same
    claim. The cap is a COST bound reached with work outstanding, and it is false here
    by construction rather than by a second expression agreeing: `confident` already
    requires `not capped`."""
    live = _finding("P2")
    d = panel.round_stop(2, 2, [live.key], [live], [])
    assert d["stop"] is True and "round cap (2) reached" in d["reason"]
    assert d["confident"] is False and d["converged"] is False


def test_a_vetoed_stop_is_not_convergence():
    """The other conjunct `confident` already carries. A veto line is the round saying
    its own quiet is not evidence of a quiet PR, and a quiet that has been disowned
    cannot also be a clean finish."""
    d = panel.round_stop(2, 5, [], [], ["claude saw 400 of 9,000 diff chars"])
    assert d["stop"] is True and d["reason"].startswith("dry")
    assert d["confident"] is False and d["converged"] is False


def test_a_below_floor_policy_stop_keeps_its_confidence_and_does_not_converge():
    """The asymmetry, and the whole of the judgement in this field. #165's floor stops
    are POLICY stops: the repo said which findings are worth a round, the round obeyed,
    and vetoing that would hand the cap back its monopoly on ending the loop. So
    `confident` stays true — and `converged` is false anyway, because the round ended
    with real findings unfixed and its `reason` says so rather than saying "dry"."""
    quiet = [_finding("P4", key_from=f"nit {i}") for i in range(3)]
    d = panel.round_stop(2, 5, [c.key for c in quiet], quiet, [],
                         trigger_floor="P2", cleared_floor="P2")
    assert d["stop"] is True and d["confident"] is True
    assert "round trigger floor" in d["reason"]
    assert d["outstanding"]["below_floor"] and d["outstanding"]["handed_to"] == "nobody"
    assert d["converged"] is False


def test_a_stop_holding_an_escalation_is_not_convergence():
    """The cycle has finished and the PR carries a question only a human closes. It is
    already non-confident through the veto that says so; `converged` names it a third
    time because this is the field a metric reads, and "the loop stopped" is not the
    thing being counted."""
    held = _finding("P2")
    d = panel.round_stop(2, 5, [], [held], [], escalated=[held.key])
    assert d["stop"] is True and "await a human" in d["reason"]
    assert d["converged"] is False


# ------------------------------------------- strictly stronger than `confident`

def test_a_repeat_under_the_trigger_floor_stops_confident_and_unconverged():
    """#621's stop, seen from this field. The round is confident — the repo's trigger
    floor said this finding is not worth another round and the round obeyed — and it
    ended with a judge-confirmed defect the fixer was told about still sitting in
    `fixable`. Counting that as a clean finish is precisely the flattery this field
    exists to prevent."""
    c = _finding("P3")
    d = panel.round_stop(2, 5, [], [c], [], repeated=[c.key],
                         trigger_floor="P2", cleared_floor="P4")
    assert d["stop"] is True and d["confident"] is True
    assert d["outstanding"]["fixable"] == [c.key]
    assert d["converged"] is False


def test_a_narrowed_finding_does_not_cost_the_round_its_convergence():
    """#615's outcome is an ANSWER, so it leaves nothing in any of the three lists and
    takes no veto line. A round whose only work was narrowed is a round with nothing
    outstanding, and it converges — which is the whole reason `narrowed` clears rather
    than merely stopping the finding counting."""
    c = _finding("P2")
    d = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], narrowed=[c.key])
    assert d["narrowed"] == [c.key]
    assert d["confident"] is True and d["converged"] is True


@pytest.mark.parametrize("rung,kwargs", [
    ("fix_injection", {"injection": None}),
    ("new_findings_not_falling", {"not_falling": None}),
    ("unrefereed_fix", {"unrefereed": None}),
])
def test_every_futility_rung_ends_the_cycle_without_converging_it(rung, kwargs):
    """The three rungs that stop a `go again` all say, in their own `reason`, that a
    human answers this rather than another fix pass — and each takes a veto line for
    it. They reach `converged` through `confident` rather than through a case of their
    own, which is what makes a rung added later inherit the safe answer."""
    news = ["k1", "k2", "k3", "k4"]
    state = {
        "fix_injection": {"injection": panel_rounds.injection_state(_counts(), 0.5)},
        "new_findings_not_falling": {
            "not_falling": panel_rounds.not_falling_state([(1, 20), (2, 20)], 1)},
        "unrefereed_fix": {"unrefereed": panel_rounds.referee_state(
            {"test": 90, "prose": 10, "production": 0, "churn": 100}, True)},
    }[rung]
    d = panel.round_stop(2, 5, news, [], [], **state)
    assert d[rung]["fired"] is True, d["reason"]
    assert d["stop"] is True and d["veto"]
    assert d["converged"] is False


def test_a_declared_premise_repeat_ends_the_cycle_without_converging_it():
    """#84's brake is the fourth, and it fires at any of the four rules rather than
    only at rule 1 — so it is worth its own case: a round it stops can be dry on every
    other measure and is still not a clean finish, because the PR carries an
    assumption two fix passes were written against."""
    d = panel.round_stop(2, 5, [], [], [], premises={
        "limit": 2, "declared": 2,
        "repeated": [{"text": "the mirror is closed", "occurrences": 2,
                      "rounds": [1, 2]}]})
    assert d["stop"] is True and "a human answers this" in d["reason"]
    assert d["converged"] is False


def test_an_unloadable_baseline_costs_the_round_its_convergence():
    """`baseline_ok` is `confident`'s fourth conjunct, and it is the one that is not
    about findings at all: a round that could not read what came before it does not
    know whether anything repeated. Inherited here rather than restated, which is the
    point of computing this FROM `confident`."""
    d = panel.round_stop(2, 5, [], [], [], baseline_ok=False)
    assert d["stop"] is True and d["reason"].startswith("dry")
    assert d["confident"] is False and d["converged"] is False


def test_the_field_is_always_present_including_on_a_go_again():
    """An absent key and "this round did not converge" are different claims, and a
    consumer forced to tell them apart would be reading the payload's age rather than
    the cycle's state — the rule every measurement block in this payload keeps."""
    for d in (panel.round_stop(1, 5, ["k1"], [_finding("P1")], []),
              panel.round_stop(2, 5, [], [], [])):
        assert isinstance(d["converged"], bool)
