"""#505's volume rung: stop when the new-finding count stops falling.

`review_panel.escalate_on.fix_injection` (#489) is already a divergence detector, and
this suite exists **beside** it rather than over it. That one asks *did the fix cause
this?* — the fraction of a round's new outstanding findings that
`panel_scope._provenance` attributed to the previous fix pass. This one asks the
question a human asked on #480, over a cycle of this board's own: *is the count still
falling?* Three rounds produced 44 findings, then 15 new, then 18 new; he stopped the
cycle and triaged the rest.

The two are not the same question and do not have the same answer. Those 18 need not
be attributable to the fix at all — a reviewer reading deeper, a seat that woke up, a
scope that widened, a vendor added mid-cycle — and `_provenance` under-counts the ones
that are, by its own documented design. So a genuinely diverging cycle can sit under
`0.5` for its whole life and be stopped only by `max_rounds`, which is a cap, and a cap
fires in the same place whether the round found two findings or twenty.

What is pinned here is the four things that make it a mechanism rather than a number,
in the shape `test_panel_injection.py` pins its sibling:

* the DIAL — 1 by default and ON, argued from `fix_injection`'s own "one round, not
  two consecutive": a count is comparable only against a predecessor, so a 2 could not
  fire before round 3 while `max_rounds` defaults to 2, and would have shipped off for
  every repo that did not configure it;
* the MEASUREMENT — a streak counted backwards over the rounds' own `new_findings`,
  where an unknown resets rather than being guessed, a flat series counts, and a noise
  floor stops 1 -> 2 from ending a cycle;
* the STOP — never dressed up as convergence, ahead of the cap in the `reason`, behind
  `fix_injection` in it, bounded to rule 1, and able to make exactly ONE transition:
  `go again` -> stop;
* the REACH — that the series the report prints is the series the gate read, through
  `run()`'s own trend rows rather than a hand-built list, and that a rebase between
  rounds cannot disarm it the way it disarms `fix_injection` (#500).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_rounds  # noqa: E402
import harness_rules  # noqa: E402

DEFAULT_BLOCK = harness_rules.DEFAULTS["review_panel"]


def _finding(severity="P2", key_from="boom", file="a.py", line=1):
    reported = [panel.Finding("claude", severity, file, line, key_from, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=line,
                           synthesis=key_from, verdict="confirmed",
                           reported_by=reported)


# ------------------------------------------------------------------------ the dial

def test_the_default_is_on_and_is_the_one_the_rules_file_documents():
    """1, and ON — the same asymmetry with #78's table that `fix_injection` and
    `premise_repeated` have, and earned the same way: it can only turn a `go again`
    into a stop, so no value of it can make a review look cleaner than it is."""
    assert DEFAULT_BLOCK["escalate_on"] == {"premise_repeated": 2,
                                            "premise_undecidable": True,
                                            "fix_injection": 0.5,
                                            "new_findings_not_falling": 1,
                                            "unrefereed_fix": True,
                                            "guard_lines": False}
    assert panel_rounds.not_falling_limit(DEFAULT_BLOCK, []) == 1


def test_a_repo_that_never_heard_of_the_key_gets_the_default():
    assert panel_rounds.not_falling_limit({}, []) == 1


def test_the_shipped_window_can_fire_before_the_shipped_cap_ends_the_cycle():
    """`fix_injection`'s own argument, one level across, and the pair it is really
    about is the window and the cap TOGETHER. A new-finding count can only be compared
    against a predecessor, so a window of N cannot fire before round N+1 — and a rung
    that cannot reach its own threshold before the cap ends the cycle is off wherever
    nobody configured it, which is the `require_failing_test` failure with the honesty
    removed. The cap was 2 when this dial shipped and is 6 now (#621), so what has to
    hold is not the literal 1 but that the two numbers still admit each other."""
    limit = panel_rounds.not_falling_limit(DEFAULT_BLOCK, [])
    cap = DEFAULT_BLOCK["max_rounds"]
    # The shortest cycle the shipped window can fire on: one round to set the count and
    # `limit` more that fail to fall below it.
    series = [(r, 9) for r in range(1, limit + 2)]
    fires = panel_rounds.not_falling_state(series, limit)
    assert fires["streak"] == limit and fires["over"] is True
    assert len(series) <= cap, (
        f"a window of {limit} needs {len(series)} rounds to fire and the cap ends the "
        f"cycle at {cap} — the rung is off on every repo that configured nothing")
    # One round short of that is NOT over, which is what says the streak is doing the
    # work above rather than the series merely being long enough.
    assert panel_rounds.not_falling_state(series[:-1], limit)["over"] is False


def test_null_switches_the_brake_off():
    """How a repo asks for the pre-#505 behaviour: the series is still recorded, still
    carried in the payload, and nothing stops on it."""
    assert panel_rounds.not_falling_limit(
        {"escalate_on": {"new_findings_not_falling": None}}, []) is None


@pytest.mark.parametrize("off", [False, ""])
def test_the_other_spellings_of_off_are_honoured_rather_than_called_typos(off):
    """`premise_repeat_limit` and `fix_injection_limit` both honour these and so does
    this: `false` is what an operator reaches for to turn a brake off, and answering
    "that is a typo" would be refusing the clearest thing they could have written."""
    assert panel_rounds.not_falling_limit(
        {"escalate_on": {"new_findings_not_falling": off}}, []) is None


def test_a_repo_that_wrote_escalate_on_keeps_the_default_for_what_it_did_not_mention():
    """`review_panel` is merged one level deep, so a written `escalate_on` REPLACES the
    default object. Without the per-key fallback, a repo naming only #84's brake would
    silently switch this one off — the failure #84 already shipped once, and the reason
    every reader in this block reads per KEY."""
    assert panel_rounds.not_falling_limit(
        {"escalate_on": {"premise_repeated": 2}}, []) == 1


@pytest.mark.parametrize("value", ["some", 0, "0", -1, 1.5, "1.5", True, [1]])
def test_a_malformed_dial_is_refused_loudly_rather_than_defaulted(value):
    """A malformed value of a key this harness KNOWS is a typo, and applying the
    default anyway runs the cycle under a policy the file did not ask for.

    `0` is the interesting one: zero consecutive not-falling rounds is satisfied by
    every round, including one whose count fell, so it is the brake switched OFF behind
    a value that reads as armed — the posture `require_failing_test` exists to refuse
    having silently. `True` is refused because a window is not a switch and there is no
    number it could mean, and because `isinstance(True, int)` would otherwise make it
    1 — the default, behind a value that means something else. `1.5` rounds is not a
    number of rounds; read quietly as 1 it would apply a policy nobody wrote."""
    with pytest.raises(SystemExit) as e:
        panel_rounds.not_falling_limit(
            {"escalate_on": {"new_findings_not_falling": value}}, [])
    assert "escalate_on.new_findings_not_falling" in str(e.value)


def test_ONE_is_accepted_here_although_premise_repeated_refuses_it():
    """The asymmetry is deliberate and is about what is being counted. A premise
    declared once is what a fix pass DOES, so `1` there would make every declaration a
    stop; a round whose new-finding count did not fall is already the whole
    observation, and there is nothing a second one would add that the first did not
    say."""
    assert panel_rounds.not_falling_limit(
        {"escalate_on": {"new_findings_not_falling": 1}}, []) == 1
    with pytest.raises(SystemExit):
        panel_rounds.premise_repeat_limit({"escalate_on": {"premise_repeated": 1}}, [])


def test_an_escalate_on_that_is_not_an_object_is_refused_here_too():
    """Refused by this reader as well as by its siblings. On every real path
    `premise_repeat_limit` has already exited — `run()` calls it first — but a function
    that relied on that would be one test double away from applying a policy nobody
    wrote."""
    with pytest.raises(SystemExit) as e:
        panel_rounds.not_falling_limit({"escalate_on": "new_findings_not_falling"}, [])
    assert "`review_panel.escalate_on`" in str(e.value)


def test_a_numeric_string_is_read_as_the_number_it_spells():
    """`premise_repeat_limit` and `fix_injection_limit` both accept the string spellings
    a hand or a templating pass writes, and all three read one block — a repo whose
    `escalate_on` came out of a generator would otherwise get a hard exit from one dial
    and a number from the next, off one file."""
    assert panel_rounds.not_falling_limit(
        {"escalate_on": {"new_findings_not_falling": "2"}}, []) == 2
    assert panel_rounds.not_falling_limit(
        {"escalate_on": {"new_findings_not_falling": " 3 "}}, []) == 3


def test_a_typo_inside_escalate_on_is_warned_about_rather_than_silently_dropped():
    """The same sweep every other nested block gets. `new_findings_not_fallng: 3` would
    otherwise leave the rung at its default with nothing on stderr."""
    unknown = harness_rules.unknown_keys(
        {"review_panel": {"escalate_on": {"new_findings_not_fallng": 3}}})
    assert unknown["review_panel.escalate_on"] == ["new_findings_not_fallng"]


def test_the_board_may_set_it_and_may_switch_it_off():
    """A futility brake a board can move but not turn off is half a policy. `either`
    for its siblings' reason: a fleet mid-drain wants a shorter window than a fleet
    reviewing one careful PR, so neither direction is the safe one."""
    dial = harness_rules.BOARD_DIALS[
        "review_panel.escalate_on.new_findings_not_falling"]
    assert (dial.kind, dial.nullable, dial.rule) == ("number", True, "either")


# ----------------------------------------------------------------- the measurement

def test_the_cycle_the_rule_was_stated_over():
    """#480, and the whole point of the rung. 44 findings, then 15 new, then 18 new:
    round 2 fell and bought round 3, round 3 did not and ends the cycle — which is
    where the human ended it."""
    assert panel_rounds.not_falling_state([(1, 44)], 1)["over"] is False
    assert panel_rounds.not_falling_state([(1, 44), (2, 15)], 1)["over"] is False
    got = panel_rounds.not_falling_state([(1, 44), (2, 15), (3, 18)], 1)
    assert (got["count"], got["was"], got["streak"], got["over"]) == (18, 15, 1, True)


def test_round_one_is_never_a_not_falling_round():
    """It has no predecessor, so there is no comparison to have failed. This is the
    structural fact the default of 1 comes out of."""
    assert panel_rounds.not_falling_state([(1, 99)], 1)["streak"] == 0
    assert panel_rounds.not_falling_state([], 1)["streak"] == 0


def test_a_FLAT_series_counts_because_flat_is_not_converging():
    """`>=`, not `>`. A cycle producing fifteen new findings a round forever is not
    converging, and a rule that only caught the rise would let it run to the cap —
    which is the failure this rung exists to remove."""
    assert panel_rounds.not_falling_state([(1, 15), (2, 15)], 1)["over"] is True


def test_a_round_below_the_noise_floor_cannot_end_a_cycle():
    """`NOT_FALLING_MIN_NEW`. One new finding then two is a rise of 100% and is a cycle
    that is very nearly done; what #505 is about is the shape where both ends of the
    comparison are volumes."""
    assert panel_rounds.NOT_FALLING_MIN_NEW == 4
    assert panel_rounds.not_falling_state([(1, 9), (2, 2)], 1)["over"] is False
    assert panel_rounds.not_falling_state([(1, 9), (2, 3)], 1)["over"] is False
    assert panel_rounds.not_falling_state([(1, 9), (2, 9)], 1)["over"] is True


def test_the_floor_is_on_BOTH_ends_of_the_comparison():
    """"Not falling" is a claim about a SERIES, and a series needs two volumes to be
    one. A round that went from one finding to four has not stopped falling — it was
    never falling, there was no volume for it to fall from.

    This is the half that is easy to leave out, and leaving it out is not a hypothetical:
    with the floor on the current round alone, `test_panel_provenance`'s "a round that
    mostly found what the last one MISSED is not diverging" — 1 finding, then 4 of which
    one was the fix pass's — was ended by this rung instead of by the cap. That round is
    exactly the false positive this rule's own docstring names, an earlier round that
    under-read, and the answer is to require the comparison to be between two
    measurements rather than to make an exception for one fixture."""
    assert panel_rounds.not_falling_state([(1, 1), (2, 4)], 1)["over"] is False
    assert panel_rounds.not_falling_state([(1, 1), (2, 400)], 1)["over"] is False
    # ...and the cycle the rule was stated over is untouched: 15 and 18 are both
    # volumes, which is the shape the rung is about.
    assert panel_rounds.not_falling_state([(1, 44), (2, 15), (3, 18)], 1)["over"] is True


def test_an_unknown_count_resets_the_streak_rather_than_being_guessed():
    """A round that reviewed nothing, a baseline that could not be read, a payload older
    than the field. An unknown is not a fall and is not a rise; it is the absence of the
    comparison, and every unknown in this module fails in the direction that does not
    stop a cycle."""
    assert panel_rounds.not_falling_state([(1, 9), (2, None), (3, 9)], 1)["streak"] == 0
    assert panel_rounds.not_falling_state([(1, 9), (2, 9), (3, None)], 1)["streak"] == 0
    # ...and the streak resumes on the far side of one, rather than being poisoned for
    # the rest of the cycle.
    assert panel_rounds.not_falling_state([(1, 9), (2, None), (3, 5), (4, 9)], 1)["streak"] == 1


def test_the_streak_is_counted_backwards_from_this_round():
    """A cycle that diverged and then converged is converging. The rung is a claim
    about where the cycle is NOW, so a pair of not-falling rounds three rounds ago
    cannot end it."""
    assert panel_rounds.not_falling_state([(1, 9), (2, 9), (3, 9), (4, 4)], 1)["streak"] == 0
    assert panel_rounds.not_falling_state([(1, 4), (2, 9), (3, 9), (4, 9)], 3)["streak"] == 3


def test_a_MISSING_round_between_two_counts_breaks_the_streak():
    """Codex's finding on this PR. Round 3 with only round 1's baseline readable —
    round 2's payload lost, or never passed — puts two counts side by side with a round
    missing between them. A missing round is missing data and must never end a cycle,
    and comparing across the gap would also make the `reason` untrue: it says "the
    round before", which across a gap is not the round before.

    The first cut compared adjacent LIST ENTRIES and said so in a comment. A decision
    documented is not a decision defended."""
    assert panel_rounds.not_falling_state([(1, 9), (3, 9)], 1)["over"] is False
    assert panel_rounds.not_falling_state([(1, 9), (2, 9), (4, 9)], 1)["over"] is False
    # ...and the streak resumes on the near side of the gap, rather than the whole
    # cycle being poisoned by one lost payload.
    assert panel_rounds.not_falling_state(
        [(1, 9), (3, 4), (4, 9), (5, 9)], 2)["streak"] == 2


def test_the_rounds_ride_beside_the_counts_so_the_gap_is_readable():
    """A reader checking `streak` against `counts` has to be able to see WHY it stopped
    where it did, and "there is a round missing here" is invisible in a bare series."""
    got = panel_rounds.not_falling_state([(1, 9), (3, 9)], 1)
    assert (got["rounds"], got["counts"]) == ([1, 3], [9, 9])


def test_a_brake_that_is_off_never_fires_however_flat_the_series():
    got = panel_rounds.not_falling_state([(1, 9), (2, 9), (3, 9)], None)
    assert got["streak"] == 2 and got["over"] is False and got["limit"] is None


def test_nothing_in_the_measurement_comes_from_provenance():
    """The property that makes this worth having rather than a tighter threshold on
    `fix_injection` (#500). That rung is computed FROM `panel_scope._provenance`, and a
    rebase between rounds destroys the range provenance is computed against — so on a
    busy queue, where most PRs are rebased mid-cycle, the one shipped convergence brake
    cannot be computed and nothing says the brake was off.

    A round's own count of its own new findings survives that, and this asserts the
    independence rather than describing it: the same series decides the same way with
    every provenance tally the panel can produce, including the empty one a disarmed
    round records."""
    verdict = panel_rounds.not_falling_state([(1, 44), (2, 15), (3, 18)], 1)
    for counts in ({}, {"introduced": 0, "missed": 0, "missed-unread": 0,
                        "unknown": 9}):
        got = panel_rounds.round_stop(
            3, 9, ["k1", "k2", "k3", "k4"], [], [],
            injection=panel_rounds.injection_state(counts, 0.5),
            not_falling=verdict)
        assert got["fix_injection"]["over"] is False   # provenance says nothing
        assert got["new_findings_not_falling"]["fired"] is True
        assert got["stop"] is True


# ------------------------------------------------------------------- the stop rule

def _flat(series=(9, 9), limit=1, first=1):
    """A `(round, count)` series starting at round `first` — the shape `run()` builds
    off the trend rows."""
    return panel_rounds.not_falling_state(
        [(first + i, n) for i, n in enumerate(series)], limit)


def test_a_cycle_whose_count_has_stopped_falling_ends():
    """The whole point. Nine new findings would buy another round under rule 1; the
    round before produced nine too, so another round buys another nine."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  not_falling=_flat())
    assert got["stop"] is True
    assert "9 new finding(s) this round against 9 the round before" in got["reason"]
    assert "escalate_on.new_findings_not_falling" in got["reason"]
    assert "a human triages what is left" in got["reason"]


def test_that_stop_is_never_reported_as_convergence():
    """The same discipline `max_fix_growth`, the round cap, a held escalation and
    #489's rung all get: a veto line naming the dial and `confident` false. A reader who
    cannot tell this from a clean finish has been told the opposite of the truth."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  not_falling=_flat())
    assert got["confident"] is False
    assert any("escalate_on.new_findings_not_falling" in v
               and "not convergence" in v for v in got["veto"])


def test_the_veto_says_the_rebase_could_not_have_disarmed_it():
    """#500, in the artifact rather than only in the comment. The two rungs' veto lines
    look alike to a reader and the one thing that distinguishes them is which is still
    armed on a PR rebased mid-cycle — which is what somebody deciding what to do about
    this stop needs to know."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  not_falling=_flat())
    line = next(v for v in got["veto"] if "new_findings_not_falling" in v)
    assert "not from provenance" in line and "#500" in line
    assert "9 -> 9" in line


def test_the_volume_stop_is_not_the_cap_and_does_not_say_it_is():
    """Both are true of this round and only one of them is actionable. A reader told
    "the counter ran out" goes looking for a bigger cap; the series says the cycle is
    not converging, which a bigger cap makes worse."""
    got = panel_rounds.round_stop(2, 2, ["k1", "k2", "k3", "k4"], [], [],
                                  not_falling=_flat())
    assert "round cap" not in got["reason"]
    assert "did not fall" in got["reason"]


def test_the_attribution_rung_is_the_more_specific_truth_and_wins_the_reason():
    """Both are volume-and-divergence bounds and both fire here. #489's NAMES the fix
    pass as the author of this round's work; this one only says the work is not
    shrinking — so the rate owns the `reason`, and both veto lines are on the record."""
    counts = {"introduced": 3, "missed": 1, "missed-unread": 0, "unknown": 0}
    got = panel_rounds.round_stop(
        2, 5, ["k1", "k2", "k3", "k4"], [], [],
        injection=panel_rounds.injection_state(counts, 0.5),
        not_falling=_flat())
    assert "introduced by the fix pass" in got["reason"]
    assert got["fix_injection"]["fired"] is True
    # ...and the one it displaced still records that it fired, which is the half a
    # payload built from `stop` alone would have lost.
    assert got["new_findings_not_falling"]["fired"] is True
    assert any("new_findings_not_falling" in v for v in got["veto"])
    assert any("escalate_on.fix_injection" in v for v in got["veto"])


def test_a_repeated_premise_still_wins_the_reason_over_both():
    """#84's brake names the ASSUMPTION a fixer wrote against twice, which is the most
    specific truth of the three. The ordering is the one `circling` already
    establishes."""
    reg = panel_rounds.new_premise_register("acme/board", 34)
    for r in (1, 2):
        panel_rounds.declare_premise(reg, "the mirror is authoritative", r, [], 2)
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  premises=panel_rounds.premise_state(reg, 2, 2),
                                  not_falling=_flat())
    assert "written against more than once" in got["reason"]
    assert any("new_findings_not_falling" in v for v in got["veto"])


def test_a_DRY_round_keeps_its_own_reason_and_its_confidence():
    """The guarantee that makes a default-on defensible, and it is CHECKED rather than
    merely obeyed: the only transition this can make is `go again` -> stop. A round with
    nothing outstanding has no next round to prevent, and rewriting its verdict as "not
    converging" would be an accusation about a cycle that converged."""
    got = panel_rounds.round_stop(2, 5, [], [], [], not_falling=_flat())
    assert got["stop"] is True and got["confident"] is True
    assert got["reason"].startswith("dry")
    assert got["new_findings_not_falling"]["over"] is True
    assert got["new_findings_not_falling"]["fired"] is False


def test_a_below_floor_policy_stop_keeps_its_own_reason_and_its_confidence():
    """#165's floor stops are POLICY stops and are deliberately NOT vetoed — the repo
    said which findings are worth a round, the round obeyed. Vetoing one through this
    door would make every configured convergence non-confident and hand the cap back its
    monopoly on ending the loop."""
    quiet = [_finding("P4", key_from=f"nit {i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 5, [c.key for c in quiet], quiet, [],
                                  trigger_floor="P2", cleared_floor="P2",
                                  not_falling=_flat())
    assert got["stop"] is True and got["confident"] is True
    assert "round trigger floor" in got["reason"]


def test_a_round_going_again_for_an_unrelated_P1_is_not_cancelled_by_the_series():
    """The rule's own scope, enforced — the same bound `fix_injection` takes and for the
    same reason. Its justification is about RULE 1, so it may only take away the round
    rule 1 was buying. Here four new findings sit below the trigger floor and the round
    goes again under rule 2 for a P1 the fix did not clear: that P1 is work the fix pass
    FAILED to do rather than work it generated, and a count over four below-floor
    findings must not cancel its repair round."""
    quiet = [_finding("P4", key_from=f"nit {i}") for i in range(4)]
    blocker = _finding("P1", key_from="the mirror never closes")
    got = panel_rounds.round_stop(2, 5, [c.key for c in quiet], [*quiet, blocker], [],
                                  trigger_floor="P2", cleared_floor="P3",
                                  not_falling=_flat())
    assert got["new_findings_not_falling"]["over"] is True
    assert got["new_findings_not_falling"]["fired"] is False
    assert got["stop"] is False and "P1/P2" in got["reason"]


def test_neither_rung_may_cancel_the_repair_round_for_an_EARLIER_ROUNDS_P1():
    """Codex's second finding on this PR, and it turned out to be about the shipped
    sibling too. "It may only take away the round rule 1 was buying" is not the same
    test as "rule 1 won the `reason`": rules 1, 2 and 3 are an if/elif chain, so a round
    with four triggering news AND a P1 an earlier round raised reports rule 1 while
    going again for both — and with `triggering` as the only condition either rung ended
    it with that P1 unfixed, which is exactly what `round_stop`'s own "a statistic may
    end the loop it is a statistic about; it may not overrule a named P1" forbids.

    The bound is shared rather than corrected on #505's rung alone: the two state the
    same rule in the same words, and two brakes whose shared sentence means two
    different things is worse than either being wrong by itself."""
    news = [_finding("P2", key_from=f"new {i}") for i in range(4)]
    blocker = _finding("P1", key_from="a P1 an earlier round raised")
    counts = {"introduced": 3, "missed": 1, "missed-unread": 0, "unknown": 0}
    got = panel_rounds.round_stop(
        2, 9, [c.key for c in news], [*news, blocker], [],
        injection=panel_rounds.injection_state(counts, 0.5),
        not_falling=_flat())
    # Rule 1 still owns the `reason` — it is the if/elif chain's first branch, and
    # that is exactly why "rule 1 won the reason" is the wrong test for the bound.
    assert got["stop"] is False and "no earlier round raised" in got["reason"]
    assert got["new_findings_not_falling"]["over"] is True
    assert got["new_findings_not_falling"]["fired"] is False
    # ...and the sibling is held to it too, which is the half that makes this a fix
    # rather than a difference between two rules that claim to be the same.
    assert got["fix_injection"]["over"] is True
    assert got["fix_injection"]["fired"] is False


def test_this_rounds_OWN_new_blockers_do_not_disarm_either_rung():
    """The other side of that bound, and the one that keeps it from gutting both rungs.
    `blockers` is every outstanding P1/P2, and on the ordinary round those ARE the news:
    four new P2s and nothing else makes it four items long. Bounded on `blockers`
    outright, neither rung could fire on the very cycle #489 was measured from — which
    is how the first cut of the fix was caught, by the end-to-end test for "a round
    whose findings are mostly its own damage" going back to ending on the cap.

    What rules 2 and 3 ask that rule 1 does not is whether there is work here the fix
    pass FAILED to do, and that is work an EARLIER round raised. This round's own new
    blockers are the news being counted."""
    news = [_finding("P1", key_from=f"new blocker {i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 9, [c.key for c in news], news, [],
                                  not_falling=_flat())
    assert got["stop"] is True
    assert got["new_findings_not_falling"]["fired"] is True


def test_neither_rung_may_cancel_the_repair_round_for_a_REPEATED_finding():
    """Rule 3's half of the same bound. A finding an earlier round already raised and
    the fixer did not clear is work the fix pass FAILED to do, not work it generated."""
    news = [_finding("P3", key_from=f"new {i}") for i in range(4)]
    stale = _finding("P3", key_from="the one the last fix missed")
    got = panel_rounds.round_stop(
        2, 9, [c.key for c in news], [*news, stale], [],
        repeated={stale.key}, not_falling=_flat())
    assert got["stop"] is False and "no earlier round raised" in got["reason"]
    assert got["new_findings_not_falling"]["over"] is True
    assert got["new_findings_not_falling"]["fired"] is False


def test_the_measurement_rides_in_the_payload_whether_it_fired_or_not():
    """Always present, `premises`' and `fix_injection`'s rule and for their reason: a
    payload with no key and a cycle with nothing to compare are different claims, and a
    consumer forced to tell them apart would be reading the payload's age rather than
    the cycle's state. `counts` carries the whole series so the streak can be checked
    against it rather than taken on trust."""
    off = panel_rounds.round_stop(2, 5, [], [], [])
    assert off["new_findings_not_falling"] == {
        "limit": None, "rounds": [], "counts": [], "count": None, "was": None,
        "streak": 0, "min_new": 4, "over": False, "fired": False}
    on = panel_rounds.round_stop(2, 5, ["k1"], [], [],
                                 not_falling=_flat((44, 15)))
    assert on["new_findings_not_falling"] == {
        "limit": 1, "rounds": [1, 2], "counts": [44, 15], "count": 15, "was": 44,
        "streak": 0, "min_new": 4, "over": False, "fired": False}


# ------------------------------------------------------------------------ the reach

def test_the_series_the_gate_reads_is_the_one_the_trend_block_prints():
    """The reach half. The counts come off `RoundTrend.new_findings`, which is the
    column #490's block renders and the payload's `cycle_trend` carries — so a reader
    checking the verdict against the block is checking it against the very series the
    verdict was taken over, rather than against a second reading that can disagree."""
    rows = [panel_rounds._trend_row(r, {"reviewed": True, "round": r,
                                        "new_findings": n})
            for r, n in ((1, 44), (2, 15), (3, 18))]
    assert [r.new_findings for r in rows] == [44, 15, 18]
    assert panel_rounds.not_falling_state(
        [(r.round, r.new_findings) for r in rows], 1)["over"] is True


def test_a_payload_that_does_not_say_WHICH_ROUND_it_is_reports_no_count():
    """Codex's third finding on this PR. `load_baseline` falls back to round 1 for a
    payload carrying no usable `round` — silently, where the field is simply absent —
    so such a row sits at `r1`, reads as consecutive with this run's round 2, and
    would let the rung end a cycle off a round number nobody read.

    A count here is a point in a SERIES and a point needs a position, so the count is
    withheld rather than the position invented; the streak already treats a withheld
    count as the absence of the comparison. The other two cells do not need this: a
    findings total is true of that payload wherever the row is put."""
    for bad in ({}, {"round": 0}, {"round": "two"}, {"round": True}):
        row = panel_rounds._trend_row(
            1, {"reviewed": True, "new_findings": 9, "to_fix": [{"severity": "P2"}],
                **bad})
        assert row.new_findings is None, bad
        # ...and the row still RENDERS at the round `load_baseline` placed it at,
        # because the block has to show the reader something. What is withheld is the
        # one number a RULE acts on; a findings total is true of that payload wherever
        # the row is put.
        assert (row.round, row.findings) == (1, 1), bad


def test_a_round_whose_BASELINE_HISTORY_is_incomplete_reports_no_count():
    """Codex's fourth finding on this PR. "New" means "no EARLIER round raised it", and
    that is a claim about the baselines — so a baseline this run could not read, or
    refused as another review's, or could not tell from a duplicate of the same round,
    makes findings an earlier round DID raise count as new. Fed to this rung that is an
    inflated count compared against a sound predecessor, which is the direction that
    ends a cycle.

    Asserted through `load_baseline`'s own refusal rather than by setting a flag, so
    what is pinned is the path `run()` actually takes."""
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        good = f"{d}/r1.json"
        with open(good, "w") as fh:
            json.dump({"round": 1, "reviewed": True, "new_findings": 9,
                       "github": "acme/board", "pr": 34, "to_fix": []}, fh)
        junk = f"{d}/r1b.json"
        with open(junk, "w") as fh:
            fh.write("{not json")
        prior = panel_rounds.load_baseline([good, junk],
                                           {"github": "acme/board", "pr": 34,
                                            "round": 2})
    assert prior.problems, "the unreadable baseline has to be reported"
    # `run()` withholds this round's cell on exactly that condition; the streak then
    # treats it as the absence of the comparison and cannot end the cycle.
    mine = None if prior.problems else 9
    rows = [*prior.trend, panel_rounds.RoundTrend(round=2, reviewed=True,
                                                  new_findings=mine)]
    got = panel_rounds.not_falling_state(
        [(r.round, r.new_findings) for r in rows], 1)
    assert got["counts"][-1] is None
    assert got["over"] is False


def test_a_round_that_reviewed_NOTHING_reports_no_count_rather_than_zero():
    """A skipped round writes `new_findings: 0` by default, and read as a real zero that
    is the strongest possible "the count fell" this block can emit — from a round that
    raised nothing because it read nothing. The gate on `reviewed` is the same one
    `introduced` and `pr_chars` take beside it."""
    row = panel_rounds._trend_row(2, {"reviewed": False, "round": 2,
                                      "new_findings": 0})
    assert row.new_findings is None


def test_a_payload_too_old_to_carry_the_field_is_unknown_rather_than_zero():
    """`_nonneg_int`'s standing rule, applied here: a hand-edited or foreign payload
    cannot make a cycle look like it converged."""
    assert panel_rounds._trend_row(
        2, {"reviewed": True, "round": 2}).new_findings is None
    assert panel_rounds._trend_row(
        2, {"reviewed": True, "round": 2, "new_findings": "lots"}).new_findings is None
    assert panel_rounds._trend_row(
        2, {"reviewed": True, "round": 2, "new_findings": -3}).new_findings is None


def test_the_payload_carries_the_column_without_the_block_growing_one():
    """A printed column needs the argument `TREND_COLUMNS`' own comment demands (#490)
    and this change does not make it. A consumer plotting a cycle should not have to
    re-read every round's file for the number the stop rule used, so the record carries
    it and the rendered block does not."""
    row = panel_rounds.RoundTrend(round=2, reviewed=True, findings=7, p1p2=1,
                                  new_findings=5, introduced=2, pr_chars=100)
    assert panel._trend_record(row, 100)["new_findings"] == 5
    assert "new" not in panel.TREND_COLUMNS
    assert "5" not in panel._trend_cells(row)
