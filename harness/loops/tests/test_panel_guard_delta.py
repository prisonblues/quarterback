"""#618: bound the guard churn ONE fix pass wrote, and print it where it can be read.

`prisonblues/lexray#1780`, five rounds. The fix passes after round 1 wrote 1,313 lines
and 848 of them — 64.6% — were test and doc, and nothing in the panel could see it.

**The statistic that was watching went the wrong way.** The panel prints
Guard-to-guarded every round, and laid side by side those five lines read 2.21 -> 2.19
-> 2.13 -> 2.09 -> 2.02 while source went 476 -> 941 and test went 883 -> 1,632. The
ratio FELL MONOTONICALLY THROUGH THE RUNAWAY IT WAS WATCHING, because a cumulative
proportion cannot tell "this change is well guarded" from "this change and its guards
are both running away" — the runaway moves numerator and denominator together. A
`max_guard_ratio` would have fired on none of those five rounds and would fire at round
1 on a heavily guarded PR that never churned at all: wrong in both directions.

Read as DELTAS the same rows say it plainly — rounds 2-5 wrote 380, 205, 205 and 58
lines of test and prose against 177, 116, 114 and 58 of production.

Four things are pinned here, in the shape `test_panel_referee.py` and
`test_panel_volume.py` pin their own rungs:

* the DIAL — `max_fix_guard_lines`, a ceiling on a PASS, and it ships `null` because
  one cycle is not a calibration. That is the assertion most at risk of quietly
  becoming a guessed constant, so it is tested against the rules file as well as the
  code;
* the MEASUREMENT — test plus prose over this round's own fix range and nothing
  earlier, so a quiet round cannot bank into a loud one;
* the ACTION — `escalate_on.guard_lines`, off, because #67 says an instrument earns a
  gate over a few dozen cycles or not at all and this one has a single PR. A set
  ceiling reports; an armed one stops;
* the TABLE — production/test/prose per pass, beside `introduced`, because a pass that
  wrote 330 test lines and a pass that wrote 330 production lines are not the same
  event and the block a reader consults for the shape of a cycle rendered them
  identically.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_propose  # noqa: E402
import panel_rounds  # noqa: E402
import panel_seats  # noqa: E402
import panel_preflight as pf  # noqa: E402
import harness_rules  # noqa: E402
from conftest import gh_stub  # noqa: E402

DEFAULT_BLOCK = harness_rules.DEFAULTS["review_panel"]
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The four fix passes of the cycle this issue was filed from, as
#: `(production, guard)` — test and doc summed, which is the quantity the ceiling is
#: on. Rounds 2-5 of `lexray#1780`.
LEXRAY_1780 = [(177, 380), (116, 205), (114, 205), (58, 58)]


def _split(production=0, test=0, prose=0, armed=False):
    """One fix pass as `referee_state` reads it, which is what the ceiling reads."""
    return panel_rounds.referee_state(
        {"production": production, "test": test, "prose": prose}, armed)


def _guard(production=0, test=0, prose=0, limit=None, armed=False):
    """One pass measured against a ceiling, as `round_stop` receives it."""
    return panel_rounds.guard_churn_state(
        _split(production, test, prose), limit, armed)


# --------------------------------------------------------------------------- the dial

def test_the_ceiling_ships_UNSET_and_the_rules_file_says_the_same():
    """**The assertion this whole change is most likely to lose.** The evidence is one
    cycle; its quiet round wrote 58 guard lines and its loud one 380, and any threshold
    drawn between them is a number chosen to fit one PR with its argument written
    afterwards — which is exactly the ceiling #67 refuses. So it ships `None`, the
    count is taken and published every round, and nothing fires until somebody writes a
    number they can defend.

    Pinned in three places at once because a default is written down twice and read
    from a third: `DEFAULTS` documents it, `panel_core` applies it, and the sample
    rules file is what a reader of this repo actually opens."""
    assert DEFAULT_BLOCK.get("max_fix_guard_lines", "absent") is None
    assert panel_core.DEFAULT_MAX_FIX_GUARD_LINES is None
    assert panel_seats.fix_guard_lines_limit({}, []) is None
    sample = json.loads((REPO_ROOT / ".harness-rules.sample").read_text())
    assert sample["review_panel"]["max_fix_guard_lines"] is None


def test_an_ABSENT_key_inherits_and_a_written_null_switches_off():
    """`fix_growth_limit`'s distinction, kept here even though the two answers coincide
    today — the day this earns a non-null default is the day it starts to matter, and a
    `.get()` collapsing both would lose the difference silently."""
    assert panel_seats.fix_guard_lines_limit({}, []) is None
    assert panel_seats.fix_guard_lines_limit({"max_fix_guard_lines": None}, []) is None
    assert panel_seats.fix_guard_lines_limit({"max_fix_guard_lines": 250}, []) == 250


@pytest.mark.parametrize("value, want", [(250, 250), (250.0, 250), (" 250 ", 250)])
def test_a_whole_number_of_lines_however_a_generator_wrote_it(value, want):
    assert panel_seats.fix_guard_lines_limit({"max_fix_guard_lines": value},
                                             []) == want


@pytest.mark.parametrize("value", [0, -1, True, False, 250.5, "many", []])
def test_a_ceiling_that_is_not_a_positive_whole_number_is_REFUSED(value):
    """`0` is the interesting one and it is refused rather than read as "a fix pass may
    write no test line at all": that is not a stricter ceiling, it is a ceiling every
    healthy pass carrying a regression test crosses, and `null` already spells what an
    operator writing `0` meant. `false` is refused with it — `isinstance(True, int)` is
    True, so the other way a hand writes "off" would otherwise become a ONE-LINE
    ceiling, which fires on every fix pass there is."""
    with pytest.raises(SystemExit):
        panel_seats.fix_guard_lines_limit({"max_fix_guard_lines": value}, [])


def test_the_ceiling_is_a_dial_the_rules_file_accepts_and_the_board_can_set():
    """A dial documented and missing from `DEFAULTS` is warned about as a typo and
    DROPPED, so a repo that set it would get default behaviour and a warning about a
    setting that exists."""
    assert not harness_rules.unknown_keys(
        {"review_panel": {"max_fix_guard_lines": 250}})
    assert "review_panel.max_fix_guard_lines" in harness_rules.BOARD_DIALS
    assert harness_rules.BOARD_DIALS["review_panel.max_fix_guard_lines"].nullable


# ------------------------------------------------------------------------- the action

def test_firing_is_an_ESCALATION_SIGNAL_before_it_is_a_STOP():
    """The decision #618 took: prefer the weaker action first. `max_fix_growth` ends a
    cycle on #188 and #236; this has one PR, and #67's rule is that an instrument earns
    a gate over a few dozen cycles or not at all. So a repo that writes a ceiling gets
    it MEASURED and REPORTED, and has to say so a second time — in a second key —
    before a round ends on it.

    The point of the pair is that neither answer is baked in: a fleet that has watched
    the count for a few dozen cycles flips one flag rather than waiting for a
    release."""
    assert DEFAULT_BLOCK["escalate_on"].get("guard_lines", "absent") is False
    assert panel_rounds.guard_lines_brake({}, []) is False
    assert panel_rounds.guard_lines_brake(
        {"escalate_on": {"guard_lines": True}}, []) is True


def test_a_repo_that_wrote_a_DIFFERENT_rung_still_gets_this_ones_default():
    """Read per KEY, not per block: `review_panel` merges one level deep, so a written
    `escalate_on` replaces the default object wholesale."""
    assert panel_rounds.guard_lines_brake(
        {"escalate_on": {"premise_repeated": 2}}, []) is False
    assert panel_rounds.guard_lines_brake(
        {"escalate_on": {"guard_lines": True, "premise_repeated": 2}}, []) is True


@pytest.mark.parametrize("value", [False, None, ""])
def test_false_and_null_are_one_value_here_as_they_are_for_its_siblings(value):
    assert panel_rounds.guard_lines_brake(
        {"escalate_on": {"guard_lines": value}}, []) is False


@pytest.mark.parametrize("value", [250, 0.5, "sometimes", []])
def test_the_THRESHOLD_may_not_be_written_here_as_well(value):
    """The number is `max_fix_guard_lines` and writing it twice is two places for one
    threshold to disagree. A number in this key is a malformed value of a known key,
    which is a hard exit rather than a silently applied default."""
    with pytest.raises(SystemExit):
        panel_rounds.guard_lines_brake({"escalate_on": {"guard_lines": value}}, [])


def test_a_malformed_escalate_on_block_is_refused_here_too():
    """This reader is public and is called directly by tests, so it does not rely on a
    sibling having validated the block first."""
    with pytest.raises(SystemExit):
        panel_rounds.guard_lines_brake({"escalate_on": "guard_lines"}, [])


def test_it_is_a_rung_the_constructive_pass_follows_like_every_other():
    """`PROPOSE_ESCALATIONS` is every BUILT rung and not a chosen subset — a rule
    covering "some escalations" is one a reader has to memorise the membership of. It
    is also the rung where the fan-out's question is nearly its own answer: the pass
    wrote more guard work than the findings asked for, and each seat is asked what the
    smallest change resolving its findings would be."""
    built = {k for k in DEFAULT_BLOCK["escalate_on"]
             if k not in panel_rounds.ESCALATE_ON_UNBUILT}
    assert built == set(panel_propose.PROPOSE_ESCALATIONS)
    assert panel_propose.escalations_fired(
        {"guard_churn": {"over": True, "fired": True}}) == ["guard_lines"]


def test_a_ceiling_a_repo_only_WATCHES_buys_no_fan_out():
    """`over` and `fired` are different questions, and reading the first as the second
    would fan a panel's worth of seats out over a round nothing stopped — which is the
    whole shipped arrangement, since the flag is off by default."""
    assert panel_propose.escalations_fired(
        {"guard_churn": {"over": True, "fired": False}}) == []


# -------------------------------------------------------------------- the measurement

def test_the_quantity_is_the_GUARD_half_of_the_pass_and_the_same_lines_the_budget_prices():
    """`test + prose`, which is #554's `unrefereed` bucket. Sharing the numerator is
    deliberate: the lines with no referee and the lines that are guard rather than
    guarded are the same lines, so a second definition here would let a repo's budget
    (`unrefereed_line_weight` prices exactly this bucket) and its ceiling count
    different things."""
    got = _guard(production=177, test=330, prose=50, limit=250)
    assert got["lines"] == 380 == _split(177, 330, 50)["unrefereed"]
    assert got["over"] is True


def test_no_ceiling_is_no_verdict_however_big_the_pass():
    """Which is every repo today. The count is still taken and still published — that
    is what "instrument before gate" means — and `limit: null` is the field a consumer
    reads to tell "under the ceiling" from "there was no ceiling"."""
    got = _guard(production=0, test=9_999, limit=None, armed=True)
    assert got["lines"] == 9_999 and got["limit"] is None and got["over"] is False


def test_the_ceiling_is_crossed_STRICTLY():
    """A pass exactly at the ceiling is under it, the reading every other ceiling in
    this file takes."""
    assert _guard(test=250, limit=250)["over"] is False
    assert _guard(test=251, limit=250)["over"] is True


def test_a_quiet_round_CANNOT_FUND_a_loud_one():
    """**The property the whole shape exists for, on the numbers it was filed from.**
    Each round reads the churn of its own fix range and nothing earlier, so nothing
    banks. At a ceiling of 250 the cycle's round-2 pass (380 guard lines) crosses and
    the other three do not — even though the four together wrote 848, which is over any
    ceiling anybody would write.

    A cumulative reading gets this exactly backwards: it reports the sum, so the loud
    round is diluted by the quiet ones around it, which is how `guard_ratio` fell every
    round of this cycle."""
    verdicts = [_guard(production=p, test=g, limit=250)["over"]
                for p, g in LEXRAY_1780]
    assert verdicts == [True, False, False, False]
    assert sum(g for _p, g in LEXRAY_1780) == 848


def test_a_pass_NOBODY_READ_reports_no_count_rather_than_a_zero():
    """#500's blindness arriving here as it arrives at the two rungs beside it. The
    VERDICT is `over: False` — a round that could not see the pass never ends a cycle
    on it — and the NUMBER is null, because `0` published for round 1 or a rewritten
    branch says a pass wrote no guard line when what happened is that nothing was
    looked at. That is the flattering direction: "wrote nothing" is the strongest
    possible version of the claim this ceiling exists to make."""
    blind = panel_rounds.guard_churn_state(
        panel_rounds.referee_state(None, False), 250, True)
    assert blind["lines"] is None and blind["over"] is False


def test_a_repo_that_did_not_ARM_the_rung_still_SEES_the_crossing():
    """`over` is a property of the MEASUREMENT and `armed` of the POLICY, kept apart on
    `referee_state`'s terms — sharpened here, because the shipped arrangement IS the
    unarmed one and a repo that set a ceiling to watch it must be able to see that a
    round crossed it."""
    watched = _guard(production=177, test=380, limit=250, armed=False)
    assert watched["over"] is True and watched["armed"] is False


# ----------------------------------------------------------------------- the stop rule

def _new(n=4):
    return [f"k{i}" for i in range(n)]


def test_an_ARMED_ceiling_ends_the_cycle_and_the_reason_names_the_number():
    got = panel_rounds.round_stop(
        2, 5, _new(), [], [],
        guard_churn=_guard(production=177, test=330, prose=50,
                           limit=250, armed=True))
    assert got["stop"] is True
    assert "380 line(s) of test and prose" in got["reason"]
    assert "`max_fix_guard_lines` ceiling of 250" in got["reason"]
    assert "a human decides" in got["reason"]
    assert got["guard_churn"]["fired"] is True


def test_an_UNARMED_ceiling_records_the_crossing_and_stops_nothing():
    """The shipped behaviour, and the escalation-signal half of the decision. The round
    goes again exactly as it would have, and the payload carries the number a human
    reads."""
    got = panel_rounds.round_stop(
        2, 5, _new(), [], [],
        guard_churn=_guard(production=177, test=330, prose=50,
                           limit=250, armed=False))
    assert got["stop"] is False
    assert got["guard_churn"]["over"] is True
    assert got["guard_churn"]["fired"] is False
    assert not any("#618" in v for v in got["veto"])


def test_that_stop_is_never_reported_as_convergence():
    """The discipline every stop in this file gets: a veto line naming what happened
    and `confident` false. This one says the ceiling is UNCALIBRATED out loud, because
    every other veto here rests on a number with a measurement behind it and a reader
    deciding what to do about this stop needs to know that this one rests on a number
    their own repo wrote."""
    got = panel_rounds.round_stop(
        2, 5, _new(), [], [], guard_churn=_guard(test=380, limit=250, armed=True))
    assert got["confident"] is False
    line = next(v for v in got["veto"] if "#618" in v)
    assert "not convergence (#618)" in line
    assert "counted over THAT PASS and nothing earlier" in line
    assert "not one anybody has calibrated" in line


def test_it_can_only_turn_a_GO_AGAIN_into_a_stop():
    """`not stop` is a condition rather than a redundancy: a dry round, a below-floor
    policy stop and a round holding an escalation each keep the reason and the
    confidence they earned, however much guard work the pass wrote."""
    got = panel_rounds.round_stop(
        2, 5, [], [], [], guard_churn=_guard(test=380, limit=250, armed=True))
    assert got["stop"] is True and got["confident"] is True
    assert got["reason"].startswith("dry")
    assert got["guard_churn"]["fired"] is False


def test_it_may_not_cancel_the_repair_round_for_a_P1_AN_EARLIER_ROUND_RAISED():
    """The second bound, and the one a codex second opinion found missing from #505's
    first draft. The argument for this rung is about rule 1 — new findings buy another
    round — so it may only take away the round rule 1 was buying. A statistic may end
    the loop it is a statistic about; it may not overrule a named P1."""
    held_over = panel.Canonical(id="x", severity="P1", file="app/a.py", line=1,
                                synthesis="a dangling handle", verdict="confirmed",
                                detail="d", reported_by=[], rationale="real")
    got = panel_rounds.round_stop(
        2, 5, _new(), [held_over], [], repeated={"x"},
        guard_churn=_guard(test=380, limit=250, armed=True))
    assert got["stop"] is False
    assert got["guard_churn"]["fired"] is False


def test_the_SHAPE_of_the_pass_is_the_more_specific_truth_than_its_SIZE():
    """`circling`'s ordering rule. #554 says the pass contained no refereed line at
    all, which is a sharper claim about the same quantity than "it wrote more guard
    lines than the ceiling allows" — so that rung owns the `reason`, and both veto
    lines are on the record."""
    got = panel_rounds.round_stop(
        2, 5, _new(), [], [],
        unrefereed=_split(production=0, test=7, prose=3, armed=True),
        guard_churn=_guard(production=0, test=7, prose=3, limit=5, armed=True))
    assert "not one of them was production code" in got["reason"]
    assert got["guard_churn"]["fired"] is True
    assert got["unrefereed_fix"]["fired"] is True
    assert any("#618" in v for v in got["veto"])
    assert any("#554" in v for v in got["veto"])


def test_it_is_the_more_specific_truth_than_the_count_not_falling():
    """The other side of that ordering: #505's rung says only that the work is not
    shrinking, and this says how much of it the last pass wrote."""
    flat = panel_rounds.not_falling_state([(1, 9), (2, 9)], 1)
    got = panel_rounds.round_stop(
        2, 5, _new(), [], [], not_falling=flat,
        guard_churn=_guard(test=380, limit=250, armed=True))
    assert "`max_fix_guard_lines`" in got["reason"]
    assert got["new_findings_not_falling"]["fired"] is True


def test_the_measurement_is_on_EVERY_round_whether_it_fired_or_not():
    """A payload with no key and a round that measured no guard churn are different
    claims, and a consumer forced to tell them apart would be reading the payload's age
    rather than the cycle's state."""
    got = panel_rounds.round_stop(2, 5, [], [], [])
    assert got.get("guard_churn", "no such block") == {
        "limit": None, "lines": None, "armed": False, "over": False, "fired": False}


# ------------------------------------------------------------------- the round table

def test_the_split_sits_BESIDE_introduced_in_the_table_anybody_reads():
    """#618's third part. `introduced` says how many of a round's findings the pass
    before it authored; nothing said what that pass DID, and a pass that wrote 330 test
    lines and a pass that wrote 330 production lines are not the same event.

    Three columns and not one combined cell, because the whole value of this block is
    that a column can be read DOWN and `177/330/50` cannot be."""
    assert panel.TREND_COLUMNS == ("round", "findings", "P1/P2", "introduced",
                                   "prod", "test", "prose", "whole PR")


def _row(round_no, findings, severe, introduced, pr_chars, churn=None):
    """One earlier round as its BASELINE PAYLOAD, read through `_trend_row`.

    Built from a payload rather than by constructing a `RoundTrend` directly, because
    the payload is the only route a real cycle takes: every row but this round's own is
    re-read from an earlier round's file, and a test that skipped that read would not
    be exercising the half of this feature that has to survive a payload written by
    another release."""
    to_fix = [{"severity": "P1" if i < severe else "P3", "key": f"r{round_no}-{i}"}
              for i in range(findings)]
    payload = {"round": round_no, "cycle": "cyc", "reviewed": True,
               "pr_chars": pr_chars, "scope": "pr", "to_fix": to_fix,
               "sonar_findings": [], "dismissed": [],
               "provenance_counts": ({} if introduced is None else
                                     {"introduced": introduced,
                                      "missed": findings - introduced,
                                      "missed-unread": 0, "unknown": 0})}
    if churn:
        production, test, prose = churn
        payload["round_stop"] = {"unrefereed_fix": {
            "churn": production + test + prose, "production": production,
            "test": test, "prose": prose}}
    return panel_rounds._trend_row(round_no, payload)


def test_the_cycle_this_was_filed_from_now_renders_its_own_shape():
    """The four passes of `lexray#1780` in the block that could not show them. Read
    down the `test` column against the `prod` column beside it: 330 against 177, then
    205 against 116. The `guard ratio` a reader had instead fell every one of these
    rounds."""
    rows = [_row(1, 8, 2, None, 113_402),
            _row(2, 14, 5, 9, 236_187, churn=(177, 330, 50)),
            _row(3, 15, 4, 13, 340_341, churn=(116, 205, 0))]
    table = [ln for ln in panel.cycle_trend_lines(rows, (1, 113_402, "pr"))]
    fence = [i for i, ln in enumerate(table) if ln == "```"]
    body = table[fence[0] + 1:fence[1]]
    assert body == [
        "round  findings  P1/P2  introduced  prod  test  prose  whole PR  vs r1",
        "   r1         8      2           —     —     —      —   113,402  1.00x",
        "   r2        14      5     9 (64%)   177   330     50   236,187  2.08x",
        "   r3        15      4    13 (87%)   116   205      0   340,341  3.00x",
    ]


def test_the_row_is_read_off_the_ROUNDS_OWN_payload_and_never_re_derived():
    """`round_stop.unrefereed_fix` has carried this split since #554. Reading it back
    rather than re-splitting a diff is what guarantees that this round's row and the
    same round's row one round later are the same numbers."""
    row = panel_rounds._trend_row(2, {
        "reviewed": True, "round": 2, "to_fix": [], "sonar_findings": [],
        "round_stop": {"unrefereed_fix": {"churn": 557, "production": 177,
                                          "test": 330, "prose": 50}}})
    got = tuple(getattr(row, kind, "no such cell")
                for kind in panel_seats.REFEREE_KINDS)
    assert got == (177, 330, 50)


@pytest.mark.parametrize("payload, why", [
    ({"reviewed": True, "round": 1, "to_fix": [], "sonar_findings": []},
     "round 1 has no fix pass in front of it"),
    ({"reviewed": True, "round": 2, "to_fix": [], "sonar_findings": [],
      "round_stop": {"unrefereed_fix": {"churn": 0, "production": 0,
                                        "test": 0, "prose": 0}}},
     "an unreadable fix range records zeros in every bucket"),
    ({"reviewed": False, "round": 2,
      "round_stop": {"unrefereed_fix": {"churn": 9, "production": 9,
                                        "test": 0, "prose": 0}}},
     "a round that reviewed nothing looked at no pass"),
])
def test_a_pass_NOBODY_READ_is_never_rendered_as_a_pass_that_wrote_nothing(payload,
                                                                           why):
    """None, never 0, and it is the flattering direction that makes it matter: `0
    production` reads as a pass that wrote no code, which is the strongest possible
    version of the very claim this instrument exists to make."""
    row = panel_rounds._trend_row(payload["round"], payload)
    got = tuple(getattr(row, kind, "no such cell")
                for kind in panel_seats.REFEREE_KINDS)
    assert got == (None, None, None), why


def test_a_question_that_does_not_ARISE_reads_differently_from_one_that_FAILED():
    """`introduced`'s own two marks, applied to the three cells beside it. Round 1 has
    no earlier fix pass, so `—`; a later round was asked and could not answer, so `?`.
    Collapsed into one mark, a cycle whose fix ranges went dark for two rounds reads
    exactly like a cycle whose passes wrote nothing."""
    rows = [_row(1, 3, 1, None, 1000), _row(2, 5, 1, None, 1200)]
    lines = panel.cycle_trend_lines(rows, (1, 1000, "pr"))
    fence = [i for i, ln in enumerate(lines) if ln == "```"]
    r1, r2 = lines[fence[0] + 2:fence[1]]
    assert r1.split()[4:7] == ["—", "—", "—"]
    assert r2.split()[4:7] == ["?", "?", "?"]


def test_the_block_says_what_the_new_columns_MEAN():
    """The block's one sentence of guidance is what stops a reader taking a column on
    its own — which is the failure `TREND_COLUMNS` refuses a density figure over."""
    rows = [_row(1, 8, 2, None, 1000), _row(2, 8, 2, 4, 2000, churn=(10, 300, 5))]
    text = "\n".join(panel.cycle_trend_lines(rows, (1, 1000, "pr")))
    assert "330 test lines" in text and "330 production lines" in text


# --------------------------------------------------------------- a whole round, end to end

def _fix_range(production: int, test: int) -> str:
    """A compare payload whose fix range wrote `production` lines of code and `test`
    lines of test — the shape the ceiling is a ceiling on."""
    files = []
    if production:
        files.append({"filename": "app/f0.py", "patch": "@@ -9,2 +9,%d @@\n context\n"
                      % (production + 2)
                      + "".join(f"+prod_{i} = {i}\n" for i in range(production))})
    if test:
        files.append({"filename": "tests/test_f0.py",
                      "patch": "@@ -9,2 +9,%d @@\n context\n" % (test + 2)
                      + "".join(f"+    assert prod_{i} == {i}\n" for i in range(test))})
    return json.dumps({"status": "ahead", "files": files})


def _diff(files: int) -> str:
    return "".join(
        f"diff --git a/app/f{i}.py b/app/f{i}.py\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/app/f{i}.py\n+++ b/app/f{i}.py\n"
        "@@ -1,1 +1,2 @@\n"
        f"+line = {i}\n" for i in range(files))


BASE_CFG = {"github": "acme/e2e", "path": "/nonexistent/acme-e2e",
            "_rules_baseline": ".harness-rules.sample",
            "reviewers": {"claude": {"enabled": True, "model": "sonnet"}}}


@pytest.fixture(autouse=True)
def every_seat_is_on_this_box(monkeypatch):
    """Pin the HOST out of every round here: `seat_ceilings` skips a seat whose CLI is
    not on PATH, so a test that leaves the real predicate in place is asserting on
    which vendor CLIs the machine running the suite happens to carry."""
    monkeypatch.setattr(pf, "seat_installed", lambda name: True)


def _round(monkeypatch, tmp_path, round_no, findings, head, compare, panel_block,
           baseline=()):
    fake_sh = gh_stub(meta={"title": "feat: mirror", "additions": 20,
                            "deletions": 2, "headRefOid": head},
                      compare=compare, diff=_diff(4))

    def fake_review(name, model, prompt, effort="", **_kw):
        return panel.ReviewerRun(
            [panel.Finding("claude", sev, f, ln, t, "detail")
             for f, ln, sev, t in findings], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None,
                        ci="", **_kw):
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1),
                                 severity=f.severity, file=f.file, line=f.line,
                                 synthesis=f.title, verdict="confirmed",
                                 detail="detail", reported_by=[f], rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None,
                panel.CoverageRuling())

    cfg = {**BASE_CFG, "review_panel": {"refuse_over_cap_multiple": 0,
                                        "manifest_moves": False, **panel_block}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=4) == 0
    return str(out), json.loads(out.read_text())


def _cycle(monkeypatch, tmp_path, panel_block, compare):
    r1_path, _ = _round(monkeypatch, tmp_path, 1,
                        [("app/f0.py", 3, "P2", "a stale mirror")],
                        head="aaa111", compare=compare, panel_block=panel_block)
    return _round(monkeypatch, tmp_path, 2,
                  [("app/f0.py", 10, "P1", "a dangling handle")],
                  head="bbb222", compare=compare, panel_block=panel_block,
                  baseline=[r1_path])


def test_a_watched_ceiling_is_REPORTED_on_the_round_and_ends_nothing(
        monkeypatch, tmp_path, capsys):
    """The shipped arrangement with a number written in it: the count crosses, the
    report says so and says which of the two things that did, and the cycle goes on."""
    _, r2 = _cycle(monkeypatch, tmp_path, {"max_fix_guard_lines": 5},
                   _fix_range(production=2, test=9))
    out = capsys.readouterr().out
    assert "**Guard churn in the last fix pass:** 9 test and prose line(s)" in out
    assert "PAST the 5-line `max_fix_guard_lines` ceiling" in out
    assert "`escalate_on.guard_lines` is off for this repo" in out
    assert r2["round_stop"]["guard_churn"]["over"] is True
    assert r2["round_stop"]["guard_churn"]["fired"] is False
    assert r2["round_stop"]["stop"] is False


def test_an_armed_ceiling_ends_the_cycle_on_the_round_that_crossed_it(
        monkeypatch, tmp_path, capsys):
    """The stronger action, one flag away. The round's only news is a P1 nobody raised
    before, so rule 1 was buying another round — and this is what takes it away."""
    _, r2 = _cycle(monkeypatch, tmp_path,
                   {"max_fix_guard_lines": 5,
                    "escalate_on": {"guard_lines": True}},
                   _fix_range(production=2, test=9))
    assert r2["round_stop"]["stop"] is True
    assert r2["round_stop"]["guard_churn"]["fired"] is True
    assert "`max_fix_guard_lines` ceiling of 5" in r2["round_stop"]["reason"]
    assert r2["round_stop"]["confident"] is False


def test_a_pass_UNDER_the_ceiling_says_so_rather_than_going_quiet(
        monkeypatch, tmp_path, capsys):
    """A line that appears only on a crossing is one a reader cannot tell from an
    instrument that stopped running. Under the ceiling it still prints the count."""
    _cycle(monkeypatch, tmp_path, {"max_fix_guard_lines": 500},
           _fix_range(production=2, test=9))
    out = capsys.readouterr().out
    assert "under the 500-line `max_fix_guard_lines` ceiling for this pass" in out


def test_no_ceiling_means_NO_LINE_because_there_is_no_policy_to_report(
        monkeypatch, tmp_path, capsys):
    """The one exception to this report's "print it at the default too" rule, and it is
    earned: the shipped value is `null` for want of a calibration, so the clause would
    otherwise report an absence on every round of every repo. The count is not lost —
    it is two of the three churn columns in the table above it."""
    _cycle(monkeypatch, tmp_path, {}, _fix_range(production=2, test=9))
    out = capsys.readouterr().out
    assert "Guard churn in the last fix pass" not in out
    assert "prod  test  prose" in out


def test_arming_the_rung_with_no_ceiling_SAYS_it_can_never_fire(
        monkeypatch, tmp_path, capsys):
    """#169's failure is a mechanism that ships unwired. A repo that armed the flag and
    wrote no ceiling has a rung that cannot fire, and the round says so rather than
    leaving it to be discovered. The reverse pair — a ceiling with the flag off — is
    the shipped arrangement and says nothing, because watching a count is what it is
    for."""
    _, r2 = _cycle(monkeypatch, tmp_path, {"escalate_on": {"guard_lines": True}},
                   _fix_range(production=2, test=9))
    said = " ".join(r2.get("config_notes") or [])
    assert "`escalate_on.guard_lines` is armed and `max_fix_guard_lines` is null" in said
    assert r2["round_stop"]["guard_churn"]["fired"] is False


def test_the_payload_and_the_table_read_ONE_measurement(monkeypatch, tmp_path):
    """The reason `referee_state` is computed above the trend block rather than beside
    the stop rule: the row, the ceiling and the payload all read one object, so a round
    cannot print a table that disagrees with its own verdict."""
    _, r2 = _cycle(monkeypatch, tmp_path, {"max_fix_guard_lines": 5},
                   _fix_range(production=2, test=9))
    split = r2["round_stop"]["unrefereed_fix"]
    row = next(t for t in r2["cycle_trend"] if t["round"] == 2)
    assert [row.get(k, "no such cell") for k in panel_seats.REFEREE_KINDS] \
        == [split[k] for k in panel_seats.REFEREE_KINDS]
    assert (r2["round_stop"].get("guard_churn") or {}).get("lines") \
        == split["unrefereed"]


def test_the_dials_line_names_the_ceiling_when_a_repo_set_one(monkeypatch, tmp_path,
                                                              capsys):
    """The orchestrator builds the fixer's brief out of this report, so what a pass is
    allowed to write has to be readable from the artifact rather than from whoever
    remembers the repo's config."""
    _cycle(monkeypatch, tmp_path, {"max_fix_guard_lines": 250},
           _fix_range(production=2, test=9))
    assert "guard 250 lines/pass" in capsys.readouterr().out


def test_the_payload_records_the_ceiling_as_APPLIED(monkeypatch, tmp_path):
    """Not as written and not as defaulted: a reader of the payload has to be able to
    see the policy the round actually ran under."""
    _, r2 = _cycle(monkeypatch, tmp_path, {"max_fix_guard_lines": 250},
                   _fix_range(production=2, test=9))
    assert r2["review_panel"].get("max_fix_guard_lines", "not recorded") == 250


# ------------------------------------------------------------------------------ docs

def test_the_README_documents_both_keys_and_why_the_ratio_is_not_one_of_them():
    """The dial table is where an operator learns what a key does, and the argument
    that matters most here is the one that says why the obvious ceiling — a cap on
    `guard_ratio` — is not what shipped."""
    readme = (REPO_ROOT / "harness/loops/README.md").read_text()
    assert "`review_panel.max_fix_guard_lines`" in readme
    assert "`review_panel.escalate_on.guard_lines`" in readme
    assert "2.21 → 2.19 → 2.13 → 2.09 → 2.02" in readme


def test_the_sample_rules_file_carries_the_argument_a_reader_of_it_needs():
    """This repo is the panel's test bench, so its own rules file doubles as the worked
    example — a reader should not have to open `harness_rules.py` to find out why a
    shipped `null` is a decision rather than an oversight."""
    sample = json.loads((REPO_ROOT / ".harness-rules.sample").read_text())
    said = sample["review_panel"].get("_618_guard_delta", "")
    assert "FELL MONOTONICALLY THROUGH THE RUNAWAY" in said
    assert "DOES NOT BANK" in said.upper()
    assert "escalate_on.guard_lines" in sample["review_panel"].get(
        "_618_guard_lines_action", "")


# --------------------------------------------------- what a second opinion caught

def test_an_ARMED_ceiling_that_did_not_END_the_round_does_not_say_it_did(
        monkeypatch, tmp_path, capsys):
    """`fired`, never `armed`. The two differ on every round the rung was bounded out
    of, and this is one: round 2's only finding is a REPEAT, so rule 3 is what buys the
    round and the rung may not take it away — it may only remove the round rule 1 was
    buying. Written off `armed`, the report claimed the cycle ended here on a round
    that went on to run."""
    same = [("app/f0.py", 3, "P2", "a stale mirror")]
    block = {"max_fix_guard_lines": 5, "escalate_on": {"guard_lines": True}}
    compare = _fix_range(production=2, test=9)
    r1_path, _ = _round(monkeypatch, tmp_path, 1, same, head="aaa111",
                        compare=compare, panel_block=block)
    _, r2 = _round(monkeypatch, tmp_path, 2, same, head="bbb222", compare=compare,
                   panel_block=block, baseline=[r1_path])
    assert r2["round_stop"]["stop"] is False
    assert r2["round_stop"]["guard_churn"]["over"] is True
    assert r2["round_stop"]["guard_churn"]["fired"] is False
    out = capsys.readouterr().out
    assert "that is what ended this cycle" not in out
    assert "this round did not end on it" in out


def test_a_round_that_read_NO_PASS_reports_no_count_and_no_line(
        monkeypatch, tmp_path, capsys):
    """Round 1 has no fix pass in front of it. A `0` there would say a pass wrote no
    guard line when what happened is that nobody looked — the flattering direction,
    since "wrote nothing" is the strongest version of the claim this ceiling makes —
    and a report line built on that number would print it."""
    _, r1 = _round(monkeypatch, tmp_path, 1,
                   [("app/f0.py", 3, "P2", "a stale mirror")], head="aaa111",
                   compare=_fix_range(production=2, test=9),
                   panel_block={"max_fix_guard_lines": 5})
    assert r1["round_stop"]["guard_churn"]["lines"] is None
    assert r1["round_stop"]["guard_churn"]["over"] is False
    assert "Guard churn in the last fix pass" not in capsys.readouterr().out
