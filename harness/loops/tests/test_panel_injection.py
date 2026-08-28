"""#489's injection gate: stop when the fix pass is generating the round's work.

From round 2 what a panel round reviews **is the previous round's fix**. So
`round_stop`'s rule 1 — new findings buy another round — is fed by the loop's own
output, and a termination test fed by its own output can only end on the cap. The
panel has measured exactly that since #48 and consumed it nowhere: `_provenance`
sorts every new finding into `introduced` / `missed` / `missed-unread` / `unknown`,
the round tallies it over the findings the cycle has to clear, the report prints
"**N introduced** by the last fix pass", and nothing read it to stop anything.

That was deliberate. `panel.py`'s recurrence comment says nothing reads these
tallies to stop a run, that #67 asks for the instrument before the gate — "two pull
requests in one day is an observation, not a calibrated rule" — and that "a few
dozen cycles of it are what would justify wiring it to anything". This suite exists
because the cycles came in: 128 of 201 new findings across seven PRs, 39 of 53 after
round 1 on PR #299 (17 of 17 in its round 2), and 64% then 87% on the cycle #489 was
filed from, over a pull request whose actual change was 113 lines.

What is pinned here is the four things that make it a mechanism rather than a
number:

* the DIAL — 0.5 by default and ON, `null` for off, and both ends of the range
  refused rather than clamped, because `1` can never be exceeded and is therefore
  the brake switched off behind a value that reads as armed;
* the MEASUREMENT — `introduced` over every new outstanding finding, with the
  unattributable buckets in the DENOMINATOR so that a round the harness could not
  place is a round that does not end a cycle, and a minimum denominator so that a
  majority of two findings cannot end one either;
* the STOP — never dressed up as convergence, ahead of the cap in the `reason`,
  behind a repeated premise in it, and able to make exactly ONE transition: `go
  again` -> stop. A dry round, a below-floor policy stop and a round holding an
  escalation keep the reason and the confidence they earned, whatever the rate;
* the REACH — that the number the payload has always carried is the number the gate
  reads, through `run()` rather than through a hand-built tally.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_rounds  # noqa: E402
import harness_rules  # noqa: E402

DEFAULT_BLOCK = harness_rules.DEFAULTS["review_panel"]


def _counts(introduced=0, missed=0, unread=0, unknown=0):
    """A `provenance_counts` object as `run()` builds it — all four buckets present,
    which is what the payload promises a consumer."""
    return {"introduced": introduced, "missed": missed,
            "missed-unread": unread, "unknown": unknown}


def _finding(severity="P2", key_from="boom", file="a.py", line=1):
    reported = [panel.Finding("claude", severity, file, line, key_from, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=line,
                           synthesis=key_from, verdict="confirmed",
                           reported_by=reported)


# ------------------------------------------------------------------------ the dial

def test_the_default_is_on_and_is_the_one_the_rules_file_documents():
    """0.5, and ON — like `premise_repeated` and unlike the rest of #78's table. The
    asymmetry is argued where the default lives: this can only turn a `go again` into
    a stop, so no value of it can make a review look cleaner than it is."""
    assert DEFAULT_BLOCK["escalate_on"] == {"premise_repeated": 2,
                                            "premise_undecidable": True,
                                            "fix_injection": 0.5,
                                            "new_findings_not_falling": 1,
                                            "unrefereed_fix": True}
    assert panel_rounds.fix_injection_limit(DEFAULT_BLOCK, []) == 0.5


def test_a_repo_that_never_heard_of_the_key_gets_the_default():
    assert panel_rounds.fix_injection_limit({}, []) == 0.5


def test_null_switches_the_brake_off():
    """How a repo asks for the pre-#489 behaviour: the rate is still measured, still
    recorded and still printed, and nothing stops on it."""
    assert panel_rounds.fix_injection_limit(
        {"escalate_on": {"fix_injection": None}}, []) is None


@pytest.mark.parametrize("off", [False, ""])
def test_the_other_spellings_of_off_are_honoured_rather_than_called_typos(off):
    """`premise_repeat_limit` honours both and so does this: `false` is what an
    operator reaches for to turn a brake off, and a harness that answered "that is a
    typo" would be refusing the clearest thing they could have written. `true` is the
    one bool that IS refused — a threshold is not a switch, and there is no number it
    could mean."""
    assert panel_rounds.fix_injection_limit(
        {"escalate_on": {"fix_injection": off}}, []) is None


def test_a_repo_that_wrote_escalate_on_keeps_the_default_for_what_it_did_not_mention():
    """`review_panel` is merged one level deep, so a written `escalate_on` REPLACES
    the default object. Without the per-key fallback, a repo naming only #84's brake
    would silently switch this one off — a governance setting turning another one off
    by omission, which is the failure #84 already shipped once."""
    assert panel_rounds.fix_injection_limit(
        {"escalate_on": {"premise_repeated": 2}}, []) == 0.5


@pytest.mark.parametrize("value", ["half", 0, 1, 1.5, -0.25, True, [0.5],
                                   float("inf"), float("nan")])
def test_a_malformed_dial_is_refused_loudly_rather_than_defaulted(value):
    """A malformed value of a key this harness KNOWS is a typo, and applying the
    default anyway runs the cycle under a policy the file did not ask for.

    `1` and `1.5` are in the list on their own merits and are the interesting half:
    the comparison is strict and a rate is at most 1.0, so neither can ever be
    exceeded. Clamped or accepted they would be the brake switched OFF behind a
    value that reads as armed, which is the posture `require_failing_test` exists to
    refuse having silently. `True` is here because a threshold is not a switch: there
    is no number it could mean, and `isinstance(True, int)` would otherwise make it
    1.0 — the brake off — while `0` would make it fire on every attributable round.
    (`False` is the one bool that is honoured; see above.)"""
    with pytest.raises(SystemExit) as e:
        panel_rounds.fix_injection_limit({"escalate_on": {"fix_injection": value}}, [])
    assert "escalate_on.fix_injection" in str(e.value)


def test_an_escalate_on_that_is_not_an_object_is_refused_here_too():
    """Refused by this reader as well as by its sibling. On every real path
    `premise_repeat_limit` has already exited — `run()` calls it first — but a
    function that relied on that would be one test double away from applying a
    policy nobody wrote."""
    with pytest.raises(SystemExit) as e:
        panel_rounds.fix_injection_limit({"escalate_on": "fix_injection"}, [])
    assert "`review_panel.escalate_on`" in str(e.value)


def test_a_typo_inside_escalate_on_is_warned_about_rather_than_silently_dropped():
    """The same sweep every other nested block gets. `fix_injecton: 0.9` would
    otherwise leave the brake at its default with nothing on stderr."""
    unknown = harness_rules.unknown_keys(
        {"review_panel": {"escalate_on": {"fix_injecton": 0.9}}})
    assert unknown["review_panel.escalate_on"] == ["fix_injecton"]


def test_the_board_may_set_it_and_may_switch_it_off():
    """A futility brake a board can move but not turn off is half a policy. `either`
    for `premise_repeated`'s reason: neither direction is the safe one."""
    dial = harness_rules.BOARD_DIALS["review_panel.escalate_on.fix_injection"]
    assert (dial.kind, dial.nullable, dial.rule) == ("number", True, "either")


# ----------------------------------------------------------------- the measurement

def test_the_rate_is_introduced_over_every_new_outstanding_finding():
    got = panel_rounds.injection_state(_counts(introduced=3, missed=1), 0.5)
    assert (got["introduced"], got["new"], got["rate"]) == (3, 4, 0.75)
    assert got["over"] is True


def test_the_unattributable_buckets_are_in_the_DENOMINATOR():
    """`unknown` and `missed-unread` depress the rate, and that is the direction a
    stop should fail in: a round the harness could not place is a round that does not
    end a cycle. Dropped from the denominator, 3 of 3 placeable findings would read
    as 100% and stop a cycle on evidence about three findings out of eight."""
    got = panel_rounds.injection_state(
        _counts(introduced=3, missed=1, unread=2, unknown=2), 0.5)
    assert (got["new"], got["rate"], got["over"]) == (8, 0.375, False)


def test_a_round_with_nothing_to_attribute_has_no_rate_at_all():
    """Round 1, or a cycle whose fix range could not be read: `run()` sends `{}` and
    the rate is null rather than 0.0. Zero is a claim about a fix pass and this is
    the absence of one — the same distinction `provenance_counts` itself draws
    between `{}` and all-zero."""
    got = panel_rounds.injection_state({}, 0.5)
    assert got["rate"] is None and got["new"] == 0 and got["over"] is False


def test_a_majority_of_two_findings_is_not_a_rate():
    """`FIX_INJECTION_MIN_NEW`. At three findings a strict majority is two of them,
    and `_provenance` is documented as routinely wrong by a line or two in both
    directions — so below the floor the cycle's verdict would be one reviewer's line
    number. 2 of 3 is 67% and does not fire; 3 of 4 does."""
    assert panel_rounds.FIX_INJECTION_MIN_NEW == 4
    assert panel_rounds.injection_state(_counts(2, 1), 0.5)["over"] is False
    assert panel_rounds.injection_state(_counts(3, 1), 0.5)["over"] is True


def test_the_threshold_is_strict_so_exactly_half_is_not_over_it():
    """"More than this fraction", as the dial is documented. Half the round's news
    being the fix pass's own is the boundary, not the offence."""
    assert panel_rounds.injection_state(_counts(2, 2), 0.5)["over"] is False
    assert panel_rounds.injection_state(_counts(3, 2), 0.5)["over"] is True


def test_a_brake_that_is_off_never_fires_however_bad_the_rate():
    got = panel_rounds.injection_state(_counts(introduced=9, missed=1), None)
    assert got["rate"] == 0.9 and got["over"] is False and got["limit"] is None


# ------------------------------------------------------------------- the stop rule

def _state(introduced=3, missed=1, limit=0.5):
    return panel_rounds.injection_state(_counts(introduced, missed), limit)


def test_a_round_generating_its_own_work_ends_the_cycle():
    """The whole point. Four new findings would buy another round under rule 1; three
    of them were written by the fix pass that preceded them, so another round buys
    another three."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_state())
    assert got["stop"] is True
    assert "3 of 4 new finding(s) (75%)" in got["reason"]
    assert "escalate_on.fix_injection" in got["reason"]
    assert "a human answers that, not another fix pass" in got["reason"]


def test_that_stop_is_never_reported_as_convergence():
    """The same discipline `max_fix_growth`, the round cap and a held escalation get:
    a veto line naming the dial and `confident` false. A reader who cannot tell this
    from a clean finish has been told the opposite of the truth."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_state())
    assert got["confident"] is False
    assert any("escalate_on.fix_injection" in v and "not convergence" in v
               for v in got["veto"])
    assert any("documented FLOOR" in v for v in got["veto"])


def test_the_injection_stop_is_not_the_cap_and_does_not_say_it_is():
    """Both are true of this round and only one of them is actionable. A reader told
    "the counter ran out" goes looking for a bigger cap; the number says the rounds
    are manufacturing their own work, which a bigger cap makes worse."""
    got = panel_rounds.round_stop(2, 2, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_state())
    assert "round cap" not in got["reason"]
    assert "introduced by the fix pass" in got["reason"]


def test_a_repeated_premise_is_the_more_specific_truth_and_wins_the_reason():
    """Both are futility bounds and both fire here. #84's names the ASSUMPTION a
    fixer wrote against twice; this one only counts findings — so the premise owns
    the `reason`, and both veto lines are on the record."""
    reg = panel_rounds.new_premise_register("acme/board", 34)
    for r in (1, 2):
        panel_rounds.declare_premise(reg, "the mirror is authoritative", r, [], 2)
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  premises=panel_rounds.premise_state(reg, 2, 2),
                                  injection=_state())
    assert "written against more than once" in got["reason"]
    assert any("escalate_on.fix_injection" in v for v in got["veto"])
    assert any("declared more than once" in v for v in got["veto"])


def test_a_DRY_round_keeps_its_own_reason_and_its_confidence():
    """The guarantee that makes a default-on defensible: the only transition this can
    make is `go again` -> stop. A round with nothing outstanding has no next round to
    prevent, and rewriting its verdict as "diverging" would be an accusation about a
    cycle that converged."""
    got = panel_rounds.round_stop(2, 5, [], [], [], injection=_state(9, 1))
    assert got["stop"] is True and got["confident"] is True
    assert got["reason"].startswith("dry")
    assert got["fix_injection"]["over"] is True


def test_a_below_floor_policy_stop_keeps_its_own_reason_and_its_confidence():
    """#165's floor stops are POLICY stops and are deliberately NOT vetoed — the repo
    said which findings are worth a round, the round obeyed. Vetoing one through this
    door would make every configured convergence non-confident and hand the cap back
    its monopoly on ending the loop, which is the failure #165 exists to remove."""
    quiet = [_finding("P4", key_from=f"nit {i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 5, [c.key for c in quiet], quiet, [],
                                  trigger_floor="P2", fix_floor="P2",
                                  injection=_state(9, 1))
    assert got["stop"] is True and got["confident"] is True
    assert "round trigger floor" in got["reason"]


def test_the_measurement_rides_in_the_payload_whether_it_fired_or_not():
    """Always present, `premises`' rule and for its reason: a payload with no key and
    a round with nothing to attribute are different claims, and a consumer forced to
    tell them apart would be reading the payload's age rather than the cycle's
    state."""
    off = panel_rounds.round_stop(2, 5, [], [], [])
    assert off["fix_injection"] == {"limit": None, "introduced": 0, "new": 0,
                                    "rate": None, "min_new": 4, "over": False,
                                    "fired": False}
    on = panel_rounds.round_stop(2, 5, ["k1"], [], [], injection=_state(1, 3))
    assert on["fix_injection"] == {"limit": 0.5, "introduced": 1, "new": 4,
                                   "rate": 0.25, "min_new": 4, "over": False,
                                   "fired": False}


def test_over_and_fired_are_different_questions_and_the_payload_keeps_them_apart():
    """A round can be over the threshold and be one this rule must not touch — a
    below-floor policy stop is the commonest. `over` says the MEASUREMENT crossed;
    `fired` says the VERDICT is this rule's. A consumer that read the first as the
    second would attach "the cycle ended on divergence" to a confident, converged
    round, which is the misreporting `round_stop` is organised against."""
    quiet = [_finding("P4", key_from=f"nit {i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 5, [c.key for c in quiet], quiet, [],
                                  trigger_floor="P2", fix_floor="P2",
                                  injection=_state(9, 1))
    assert got["fix_injection"]["over"] is True
    assert got["fix_injection"]["fired"] is False
    assert got["confident"] is True


def test_a_round_going_again_for_an_unrelated_P1_is_not_cancelled_by_the_rate():
    """The rule's own scope, enforced. Its justification is about RULE 1 — new
    findings buying another round, fed by the loop's own output — so it may only take
    away the round rule 1 was buying.

    Here four new findings sit below the trigger floor, so rule 1 buys nothing, and
    the round goes again under rule 2 for a P1 the fix did not clear. That P1 is work
    the fix pass FAILED to do rather than work it generated, and a statistic computed
    over four below-floor findings must not cancel its repair round. This is where the
    rule parts company with #84's brake, which fires at any of the four rules: a
    repeated premise is a fixer's own declaration, and this is a threshold."""
    quiet = [_finding("P4", key_from=f"nit {i}") for i in range(4)]
    blocker = _finding("P1", key_from="the mirror never closes")
    got = panel_rounds.round_stop(2, 5, [c.key for c in quiet], [*quiet, blocker], [],
                                  trigger_floor="P2", fix_floor="P3",
                                  injection=_state(3, 1))
    assert got["fix_injection"]["over"] is True
    assert got["fix_injection"]["fired"] is False
    assert got["stop"] is False and "P1/P2" in got["reason"]


def test_a_numeric_string_is_read_as_the_number_it_spells():
    """`premise_repeat_limit` and `fix_growth_limit` both accept the string spellings
    a hand or a generator writes, and this reads the same block as the first of them —
    a repo whose `escalate_on` came out of a templating pass would otherwise get a
    hard exit from one dial and a number from the other, off one file."""
    assert panel_rounds.fix_injection_limit(
        {"escalate_on": {"fix_injection": "0.75"}}, []) == 0.75
    assert panel_rounds.fix_injection_limit(
        {"escalate_on": {"fix_injection": " 0.5 "}}, []) == 0.5


def test_a_board_dial_over_a_replaced_escalate_on_is_REPORTED_not_silent():
    """The known edge of the per-key fallback, pinned rather than left to be
    discovered. `escalate_on` merges wholesale, so a repo writing only
    `premise_repeated` has no `fix_injection` leaf for a board dial to overwrite —
    and `apply_dials` refuses to create one, deliberately: setting it back would
    resurrect a key from a layer the repo cannot see.

    The reader's fallback still supplies the DEFAULT, so the brake is on at 0.5 while
    the board believes its own value is in force. That is the same shape
    `premise_repeated` has had since #84 and the reason it is survivable is the one
    asserted here: the refusal is in `problems`, named, rather than silent."""
    cfg = {"review_panel": {"escalate_on": {"premise_repeated": 2}}}
    applied, problems = harness_rules.apply_dials(
        cfg, {"review_panel.escalate_on.fix_injection":
              {"value": None, "scope": "repo"}})
    assert not applied
    assert any("review_panel.escalate_on.fix_injection" in p
               and "nothing to override" in p for p in problems)
    # And the reader is unmoved by the board's opinion, which is the half that makes
    # the report load-bearing rather than decorative.
    assert panel_rounds.fix_injection_limit(cfg["review_panel"], []) == 0.5
