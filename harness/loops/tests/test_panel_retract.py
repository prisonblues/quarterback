"""#674: a declination a human has retracted, and the landing it gives back.

#665 gave a fix pass a way to record a correction it could not make, so the next
round inherits the fact instead of paying to rediscover it. It gave nobody a way
to say the correction has since been made. That makes the register a one-way
door, and the cost is not the one #665 wrote down.

The chain, which is what these tests are written around: a stopped round holding
a declination appends a veto; `confident` requires an empty veto, so the round
reports `stop_confident: false`; and `preland`'s `_round_stop_earned` turns a
false `stop_confident` into a FAILED check rather than a warning under
`--require-earned-stop` — the mode `/panel-review-pr` §7 runs when it is about to
offer to land. So one declination held a PR out of a strict landing for the rest
of the cycle, with a fresh cycle the only exit, and a `--declined` key that named
no finding at all did it just as effectively as a real one.

`--retract` is the fourth register of the shape `escalated` (#221),
`acknowledged` (#547) and `declined` (#665) already have, and the only one that
CANCELS another. It is deliberately a human act. A fix pass reporting that it
fixed the thing is the actor attesting to its own work (#622), and a finding
being absent from a later round is not evidence of a repair when that round's
scope never re-read the file — which is the same reason #665 bounded its own
register the way it did.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel_rounds  # noqa: E402

#: A key of the right shape that names nothing — 16 hex characters, which is what
#: `_is_key` accepts and what a finding's own key looks like.
STRANGER = "a0b1c2d3e4f56789"
OTHER = "b1c2d3e4f5a67890"


def _inherited(raw, was=2, path="b.json"):
    b = panel_rounds.Baseline()
    panel_rounds._inherit(b.retracted, raw, was, path, b.problems,
                          "retracted", "retraction", panel_rounds._is_key,
                          lambda k: k.strip().lower(), "the shape of a finding key",
                          "cost")
    return b


# ---- the consequence, which is the whole point -----------------------------

def test_a_declination_costs_the_round_its_earned_stop():
    """The state `--retract` exists to leave. Not an assertion about the flag —
    an assertion that the thing it lifts is worth lifting."""
    stop = panel_rounds.round_stop(2, 6, [], [], [], True, declined=[STRANGER])
    assert stop["confident"] is False
    assert any("could not make" in v for v in stop["veto"]), stop["veto"]


def test_lifting_the_declination_gives_the_earned_stop_back():
    """The same round with the register emptied — which is exactly what the
    subtraction in `panel.run` produces from a `--retract`."""
    stop = panel_rounds.round_stop(2, 6, [], [], [], True, declined=[])
    assert stop["confident"] is True
    assert not [v for v in stop["veto"] if "could not make" in v]


def test_retracting_one_of_two_does_not_lift_the_other():
    """The veto is about the register, not about any one key, so a cycle holding
    two declinations and retracting one is still held — and must say so."""
    stop = panel_rounds.round_stop(2, 6, [], [], [], True, declined=[OTHER])
    assert stop["confident"] is False
    assert any("1 correction(s)" in v for v in stop["veto"]), stop["veto"]


# ---- the register travels the way the other three do -----------------------

def test_a_retraction_carries_the_round_it_was_made_in():
    got = _inherited({STRANGER: 1})
    assert got.retracted == {STRANGER: 1}
    assert got.problems == []


def test_the_key_is_normalised_the_way_a_finding_spells_its_own():
    got = _inherited({STRANGER.upper(): 1})
    assert got.retracted == {STRANGER: 1}


def test_a_key_of_the_wrong_shape_is_reported_and_not_inherited():
    """`_inherit`'s own rule, and the reason this register reuses it rather than
    re-implementing the failure handling: a retraction that matches nothing must
    not sit in the register looking honoured."""
    got = _inherited({"not-a-key": 1})
    assert got.retracted == {}
    assert got.problems, "a malformed retraction must be reported, not dropped"


def test_two_baselines_keep_the_earliest_round_that_retracted():
    """The same rule the three registers beside it use. Re-passing an inherited
    retraction must not re-date it to now, or the record stops saying when the
    human actually made the call."""
    b = panel_rounds.Baseline()
    for was, raw in ((3, {STRANGER: 3}), (1, {STRANGER: 1})):
        panel_rounds._inherit(b.retracted, raw, was, "b.json", b.problems,
                              "retracted", "retraction", panel_rounds._is_key,
                              lambda k: k.strip().lower(), "shape", "cost")
    assert b.retracted == {STRANGER: 1}


def test_the_field_defaults_empty_so_a_round_can_say_nobody_retracted_anything():
    """An absent register and an empty one must not be the same value, which is
    why the payload emits it even when nothing was retracted."""
    assert panel_rounds.Baseline().retracted == {}
