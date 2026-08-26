"""#506: when the fix pass is what generated the round's work, name it and price
undoing it.

`escalate_on.fix_injection` (#489) already ends the cycle when more than half a
round's new outstanding findings were attributed by `panel_scope._provenance` to
the pass immediately before them. Ending it is right and it is half an answer:
**the fix pass that caused the damage is still on the branch**. The PR then ships
carrying a change the panel has just finished saying generated more work than the
pull request did, minus the round that would have found the rest of it. Stopping
means the loop no longer makes it worse; it does not make it better. In every one
of the measured cycles — 128 of 201 new findings across seven PRs, 64% then 87% on
the cycle #489 was filed from over a 113-line change — the outcome was a stop with
the injected complexity left in place.

A stop says *we ran out of confidence*. A revert says *we know which change made it
worse*, which is a much stronger claim and needs attribution to make it. #489 is
that attribution arriving calibrated, and this is the first thing built on it that
acts on WHICH change was at fault rather than on how the round ended.

What is pinned here is the four things that make it a proposal rather than a robot
with a `git revert`:

* the RANGE — the same one provenance attributed against, so the proposal cannot
  point at a different pass from the one the rate accused, with the command spelled
  out and run by nothing;
* the TWO COLUMNS — what a revert would remove against what it would cost — and
  their opposite biases, which are the whole defence of offering it at all: the cost
  is an upper bound and the benefit is a lower one, so the argument AGAINST reverting
  always gets the benefit of the doubt;
* the SILENCE where it cannot be said — #500's rebase case, in #500's own words
  rather than in a second vocabulary, because the range that would name the offending
  pass is the range a rewrite removes;
* the FACT THAT IT DECIDES NOTHING. It cannot stop a cycle and cannot buy one another
  round. Every other argument to `round_stop` can move `stop`; this one may not, and
  that is asserted rather than assumed.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_rounds  # noqa: E402
import panel_scope  # noqa: E402
import panel_preflight as pf  # noqa: E402
from conftest import gh_stub  # noqa: E402


def _finding(severity="P2", key_from="boom", file="a.py", line=1):
    reported = [panel.Finding("claude", severity, file, line, key_from, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=line,
                           synthesis=key_from, verdict="confirmed",
                           reported_by=reported)


def _counts(introduced=0, missed=0, unread=0, unknown=0):
    return {"introduced": introduced, "missed": missed,
            "missed-unread": unread, "unknown": unknown}


def _injection(introduced=3, missed=1, limit=0.5):
    return panel_rounds.injection_state(_counts(introduced, missed), limit)


def _shape(merges=0, commits=2, total=None):
    """`fix_pass_commits`' answer for a range: what the pass is made of, how many of
    those are merges, and whether the whole range came back — the three fields that
    together decide whether a command may be offered."""
    listed = [{"sha": f"c{i}" * 8, "title": f"fix: the {i}th thing"}
              for i in range(commits)]
    total = commits if total is None else total
    return {"commits": listed, "merges": merges, "total": total,
            "complete": total == commits}


def _armed(**kw):
    """A proposal a round could actually put: a readable range with both ends, over a
    pass whose commits are known to be the fixer's own."""
    args = dict(base_sha="aaa11111", head_sha="bbb22222", head_round=1,
                scope="pr", shape=_shape(),
                removes=[{"key": "k1", "severity": "P3", "file": "a.py",
                          "line": 4, "title": "the fix dropped the lock"}],
                costs=[{"key": "old", "severity": "P1", "file": "a.py",
                        "line": 9, "title": "a stale mirror"}])
    args.update(kw)
    return panel_rounds.revert_state(panel_scope.FIX_RANGE_OK, **args)


# ------------------------------------------------- what the pass actually achieved

def test_a_complaint_the_round_no_longer_raises_is_what_a_revert_would_COST():
    """The cost column. The anchor round sent its fixer to two defects and this round
    carries neither, so undoing the pass hands both back — that is the case against
    reverting, and it has to be counted before anything proposes one."""
    was = [("k-old", "P1", "a.py", 9, "a stale mirror"),
           ("k-two", "P2", "b.py", 3, "the retry never backs off")]
    cleared, still_open = panel_rounds.fix_pass_outcome(was, [_finding(key_from="new")])
    assert [r["key"] for r in cleared] == ["k-old", "k-two"]
    assert still_open == []
    assert cleared[0] == {"key": "k-old", "severity": "P1", "file": "a.py",
                          "line": 9, "title": "a stale mirror"}


def test_a_complaint_the_pass_did_NOT_clear_costs_nothing_to_revert():
    """The other half, and the one that keeps the proposal honest. A finding the fix
    pass was sent to and left outstanding is outstanding either way — reverting does
    not lose it, because it was never gained. Counted as a cost it would inflate the
    argument against every revert by however badly the pass performed."""
    still = _finding(key_from="a stale mirror", file="a.py", line=9)
    was = [(still.key, "P1", "a.py", 9, "a stale mirror"),
           ("k-two", "P2", "b.py", 3, "the retry never backs off")]
    cleared, still_open = panel_rounds.fix_pass_outcome(was, [still])
    assert [r["key"] for r in cleared] == ["k-two"]
    assert [r["key"] for r in still_open] == [still.key]


def test_the_cost_is_measured_on_KEYS_alone_so_it_OVERSTATES_rather_than_understates():
    """A decision, pinned so the next reader sees one rather than a gap.

    `Baseline.raised_before` has a reworded-title fallback and this deliberately does
    not reuse it. There, a wrong "already raised" deletes a finding from a fixer's
    brief, so the fallback earns its complexity. Here the same match would move a
    finding OUT of the cost column and shrink the downside of the revert this function
    exists to price — the one direction a proposal must never fail in.

    So a defect the panel re-worded between rounds reads as cleared, the cost comes
    out too high, and the case against reverting keeps the benefit of the doubt. The
    two titles below are the same defect in different words, and their keys differ."""
    reworded = _finding(key_from="the mirror is stale", file="a.py", line=9)
    was = [("k-old", "P1", "a.py", 9, "a stale mirror")]
    cleared, still_open = panel_rounds.fix_pass_outcome(was, [reworded])
    assert [r["key"] for r in cleared] == ["k-old"] and still_open == []


def test_an_anchor_round_that_asked_for_nothing_prices_at_nothing():
    """Round 1 of a cycle whose baseline carries no `to_fix`, or a payload written
    before the field. No complaints means no cost, which is not the same as a cost of
    zero being asserted about a pass nobody can see — `revert_state.kind` is what
    carries that distinction."""
    assert panel_rounds.fix_pass_outcome([], [_finding()]) == ([], [])


# --------------------------------------------------------------- the proposal itself

def test_an_armed_proposal_names_the_range_and_spells_the_command_out():
    """The point of attribution: a bounded set of commits, named, with the invocation
    written down so a human acts on it without deriving anything from two SHAs. The
    range is `prior.head_sha .. head_sha` — the same one provenance attributed
    against, so the proposal cannot accuse a different pass from the rate."""
    got = _armed()
    assert got["kind"] == panel_scope.FIX_RANGE_OK
    assert got["range"] == "aaa11111..bbb22222"
    assert got["command"] == "git revert --no-commit aaa11111..bbb22222"
    assert got["no_command"] is None
    assert (got["base"], got["head"], got["round"]) == ("aaa11111", "bbb22222", 1)
    assert got["commit_count"] == 2 and len(got["commits"]) == 2


def test_nothing_here_RUNS_the_revert():
    """The load-bearing constraint, asserted where it can be. Reverting a pass reverts
    the real fixes in it too, and nothing in this module knows which those are without
    asking — so the command is a string in a payload and there is no code path from it
    to a subprocess."""
    got = _armed()
    assert isinstance(got["command"], str)
    # The command is a string in a payload and nothing in `panel_rounds` shells out —
    # `panel_core.sh` is the package's only door to a subprocess, and this module
    # never reaches it.
    src = Path(panel_rounds.__file__).read_text()
    body = src.split("def revert_state", 1)[1].split("\ndef ", 1)[0]
    assert "sh(" not in body and "subprocess" not in body


# ------------------------------------------- what makes the COMMAND safe to offer
#
# Both of these are Codex's findings on this change's first cut, and they are the same
# defect seen twice: naming a range is not the same as knowing that reverting the range
# is the right action. A proposal that hands somebody a command has to be right about
# that, because the command is the half they will paste.

def test_a_MERGE_inside_the_range_withholds_the_command_and_says_why():
    """`git revert A..B` refuses a merge commit outright without `-m`, so the offered
    invocation could not run as written — and a merge is *how the base branch gets into
    the range* in the first place. `_fix_range_diff` already documents that lean for
    attribution, where it over-counts `introduced`; here the same range would propose
    undoing other people's commits, which is not a smaller version of the right action.

    The RANGE is still named, because that is #506's requirement and naming it costs
    nothing. It is only the paste-and-run half that is held back."""
    got = _armed(shape=_shape(merges=1, commits=3))
    assert got["range"] == "aaa11111..bbb22222"        # still named
    assert got["command"] is None
    assert "merge commit" in got["no_command"] and "`-m`" in got["no_command"]
    assert "undo commits no fix pass wrote" in got["no_command"]


def test_a_shape_that_could_not_be_READ_withholds_the_command_too():
    """`{}` from `fix_pass_commits` means the commits could not be listed — no `gh`, an
    API refusal, a body that did not parse. "We did not check" must not render as "we
    checked and it is clean": the failure directions are not symmetric, since the whole
    point of the check is to catch a range that must not be reverted wholesale."""
    got = _armed(shape={})
    assert got["range"] == "aaa11111..bbb22222" and got["command"] is None
    assert "could not be listed" in got["no_command"]
    got_none = _armed(shape=None)
    assert got_none["command"] is None and got_none["no_command"] is not None


def test_a_zero_merge_count_over_a_TRUNCATED_range_is_not_a_clean_range():
    """Codex's second pass, and it is the same lesson one level down. GitHub's compare
    returns at most 250 commits and names the real figure in `total_commits`, so on a
    longer range the merge count is a FLOOR — a merge past the ceiling is invisible —
    and `merges == 0` there means "none seen", not "none". Read as clean it would hand
    out the unsafe command the check above exists to withhold.

    `complete` is what tells the two zeroes apart, and it is required rather than
    merely consulted: a shape that does not carry it withholds the command."""
    got = _armed(shape=_shape(merges=0, commits=250, total=900))
    assert got["command"] is None
    assert "merge count is a floor" in got["no_command"]
    assert "900 commit(s)" in got["no_command"] and "only 250" in got["no_command"]
    # And a shape from an older harness, or a hand-built one, is not assumed complete.
    assert _armed(shape={"commits": [], "merges": 0})["command"] is None


def test_completeness_is_decided_where_the_range_is_read_not_where_it_is_proposed():
    """`fix_pass_commits` compares `total_commits` against what it actually received,
    so the proposal reads one flag instead of re-deriving the comparison from two
    numbers it would have to keep in step. A missing `total_commits` comes back through
    the `--jq` as `0`, which must read as "not verified" rather than as complete —
    that is why the test is `==` and not `<=`."""
    import panel_core as pc

    def answering(body):
        def fake(args, **kw):
            return body
        return fake

    full = json.dumps({"total": 2, "commits": [
        {"sha": "a" * 40, "title": "fix: one", "parents": 1},
        {"sha": "b" * 40, "title": "fix: two", "parents": 1}]})
    short = json.dumps({"total": 900, "commits": [
        {"sha": "a" * 40, "title": "fix: one", "parents": 1}]})
    nameless = json.dumps({"total": 0, "commits": [
        {"sha": "a" * 40, "title": "fix: one", "parents": 1}]})

    old = pc.sh
    try:
        for body, want in ((full, True), (short, False), (nameless, False)):
            pc.sh = answering(body)
            assert panel_scope.fix_pass_commits(
                "acme/e2e", "aaa11111", "bbb22222")["complete"] is want
    finally:
        pc.sh = old


def test_a_range_that_covers_more_than_ONE_fix_pass_says_how_many():
    """Codex's third finding. `Baseline.head_sha` is the latest earlier round that
    SUPPLIED a commit, not the latest that ran — a round whose payload recorded none
    leaves the next one anchored further back, and the range is then two fix passes
    while the sentence calls it "the fix pass".

    Reported and not refused, unlike a merge, and the difference is which claim goes
    wrong. A merge makes the offered command wrong. A wide span does not: the range is
    exactly the one provenance attributed over, so the rate accused every commit in it
    and so does the proposal. What it makes wrong is the word "pass", singular."""
    got = panel_rounds.round_stop(3, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(),
                                  revert=_armed(head_round=1, round_no=3))
    assert got["revert"]["spans"] == 2
    remedy = next(v for v in got["veto"] if "#506" in v)
    assert "covers 2 fix passes rather than one" in remedy
    assert "the rate was computed over all of it too" in remedy
    # The command still stands: the range is the one that was accused.
    assert got["revert"]["command"] == "git revert --no-commit aaa11111..bbb22222"


def test_the_ordinary_adjacent_round_does_not_carry_that_note():
    """A caveat printed on every round is one the reader skips on the round it
    matters, and round N anchored on round N-1 is the ordinary case."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(),
                                  revert=_armed(head_round=1, round_no=2))
    assert got["revert"]["spans"] == 1
    assert "fix passes rather than one" not in next(
        v for v in got["veto"] if "#506" in v)


def test_an_unknown_span_is_None_and_not_ONE():
    """A round that was not told its own number, or an anchor that recorded no round.
    "One fix pass" is a claim, and this is the absence of one — the same distinction
    `rate: null` draws against `rate: 0.0`."""
    assert _armed(round_no=None)["spans"] is None
    assert _armed(head_round=None, round_no=3)["spans"] is None
    # And an anchor that is not EARLIER than this round says nothing rather than a
    # negative: a baseline claiming this round or a later one is refused upstream, and
    # inventing an answer here would hide it if that ever stopped being true.
    assert _armed(head_round=3, round_no=3)["spans"] is None


def test_the_command_carries_FULL_shas_and_the_label_carries_short_ones():
    """A display span is read by a person; a command is executed by git. Eight hex
    characters is a fine label and an abbreviation that can be ambiguous in a real
    repository — or simply absent from it — so the two must not be the same string."""
    got = _armed(base_sha="a" * 40, head_sha="b" * 40)
    assert got["range"] == "aaaaaaaa..bbbbbbbb"
    assert got["command"] == f"git revert --no-commit {'a' * 40}..{'b' * 40}"


def test_the_veto_prints_the_reason_in_the_commands_place_rather_than_the_command():
    """Printing a command that cannot run, with a caveat beside it, invites the paste.
    The line says what is in the way instead, and still says everything else — the
    range, the pass's size, and both columns — because the decision is unchanged; only
    the mechanical shortcut is."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(),
                                  revert=_armed(shape=_shape(merges=1, commits=3)))
    remedy = next(v for v in got["veto"] if "#506" in v)
    # No PASTEABLE invocation. `git revert` still appears as prose, naming the tool
    # that refuses a merge — which is the explanation, not an instruction.
    assert "git revert --no-commit" not in remedy
    assert "no wholesale command is offered here" in remedy
    assert "The pass is 3 commit(s)." in remedy
    assert "aaa11111..bbb22222" in remedy and "REMOVE the 1 finding(s)" in remedy
    # It is still a proposal that was made: the range is named and the columns priced.
    assert got["revert"]["offered"] is True


def test_a_merge_PAST_the_display_cap_still_counts():
    """The listing is truncated and the merge count is not. Counted only over the
    commits that fit, a long fix pass with a base-branch merge at the far end would
    report as clean *because it was long*, which is the failure this check exists to
    prevent arriving through the door meant to keep a payload small."""
    assert panel_scope.FIX_PASS_COMMIT_CAP == 20
    raw = [{"sha": f"{i:040x}", "title": f"c{i}", "parents": 1} for i in range(25)]
    raw[24]["parents"] = 2
    monkey = json.dumps({"total": 25, "commits": raw})
    seen = {}

    def fake(args, **kw):
        seen["args"] = args
        return monkey

    import panel_core as pc
    old, pc.sh = pc.sh, fake
    try:
        got = panel_scope.fix_pass_commits("acme/e2e", "aaa11111", "bbb22222")
    finally:
        pc.sh = old
    assert got["merges"] == 1
    assert got["total"] == 25 and len(got["commits"]) == 20
    # The DISPLAY cap is not incompleteness: all 25 came back and were counted over,
    # only 20 are listed. `complete` is about what GitHub returned, not about what this
    # payload prints, and conflating them would withhold the command on every pass
    # longer than twenty commits.
    assert got["complete"] is True


def test_listing_the_commits_never_takes_a_round_down():
    """`compare_facts`' contract, and for its reason: this is an assurance about a
    proposal nothing acts on, so no failure of it may kill a round that has already
    made the attribution. Every exception is the empty answer, which withholds the
    command."""
    import panel_core as pc

    def boom(args, **kw):
        raise OSError("no gh on PATH")

    old, pc.sh = pc.sh, boom
    try:
        assert panel_scope.fix_pass_commits("acme/e2e", "aaa11111", "bbb22222") == {}
    finally:
        pc.sh = old
    # And the degenerate inputs never reach a subprocess at all.
    assert panel_scope.fix_pass_commits("", "a", "b") == {}
    assert panel_scope.fix_pass_commits("acme/e2e", "aaa", "aaa") == {}


def test_a_REBASED_round_cannot_name_the_pass_and_says_so_in_500s_words():
    """#500's constraint arriving where it bites. A rewrite between rounds removes the
    range provenance reads — and it is the same range that would identify the offending
    pass, so this feature goes dark with the gate rather than guessing at a commit
    span or returning an empty proposal that reads like "nothing to propose".

    The vocabulary is #500's own (`panel_scope.FIX_RANGE_BLIND`) and not a second one,
    because two spellings of "we cannot see this" is two answers to one question."""
    got = panel_rounds.revert_state(
        panel_scope.FIX_RANGE_BLIND,
        why="aaa11111..bbb22222 have diverged — the branch was rewritten between rounds",
        base_sha="aaa11111", head_sha="bbb22222", head_round=1)
    assert got["kind"] == panel_scope.FIX_RANGE_BLIND
    assert got["range"] is None and got["command"] is None
    assert "rewritten between rounds" in got["why"]


def test_a_VACUOUS_round_is_told_apart_from_a_blind_one_here_too():
    """`no-fix` and `blind` both come back with no range and they are not the same
    news (#500). Nothing landed between the rounds, so there is no pass to propose
    undoing; against that, a pass landed and cannot be seen. Collapsing them would
    make an honest empty round read as a lost instrument."""
    got = panel_rounds.revert_state(panel_scope.FIX_RANGE_NO_FIX,
                                    why="no commit landed between rounds",
                                    base_sha="aaa11111", head_sha="aaa11111")
    assert got["kind"] == panel_scope.FIX_RANGE_NO_FIX and got["range"] is None


def test_round_one_is_NOT_ASKED_rather_than_blind():
    """There is no earlier round, so there is no fix pass between two rounds to have a
    range with. That is a fact about the cycle and not a failure to read anything —
    the same distinction `provenance_counts` draws between `{}` and all-zero."""
    got = panel_rounds.revert_state(panel_rounds.REVERT_NOT_ASKED)
    assert got["kind"] == "not-asked"
    assert (got["range"], got["command"], got["why"]) == (None, None, None)


def test_the_cost_column_survives_a_range_nothing_can_read():
    """It does not come FROM the range — it comes from the anchor round's own brief —
    and "here is what the pass this cannot name was sent to do" is worth more to an
    operator than a blank. `removes` is the opposite case and is empty by construction
    on such a round: it is the `introduced` bucket, and a blind round has none."""
    got = panel_rounds.revert_state(
        panel_scope.FIX_RANGE_BLIND, why="the branch was rewritten between rounds",
        costs=[{"key": "old", "severity": "P1", "file": "a.py", "line": 9,
                "title": "a stale mirror"}])
    assert [r["key"] for r in got["costs"]] == ["old"] and got["removes"] == []


def test_a_severity_census_is_worst_first_and_names_what_it_cannot_rank():
    """The one line a human weighs a revert on says `3×P3` and not `3 findings`: a
    pass that cleared two P1s and introduced three P3s is a very different proposal
    from the reverse, and the counts alone cannot tell them apart. A severity outside
    `SEVERITIES` — another harness's payload, a Sonar issue whose severity did not map
    — sorts last rather than being dropped from a census somebody is deciding on."""
    assert panel_rounds._by_severity(
        [{"severity": "P3"}, {"severity": "P1"}, {"severity": "P3"}]) == "1×P1, 2×P3"
    assert panel_rounds._by_severity([{"severity": "BLOCKER"},
                                      {"severity": "P2"}]) == "1×P2, 1×BLOCKER"
    assert panel_rounds._by_severity([]) == ""


# ------------------------------------------------------- what the round does with it

def test_the_round_that_stops_on_injection_puts_the_proposal_in_the_veto():
    """The whole feature in one assertion. #489's line ends the cycle; this one says
    what to do about the change that ended it, and says the thing the cycle's own stop
    cannot: that the change is STILL THERE."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(), revert=_armed())
    assert got["stop"] is True and got["revert"]["offered"] is True
    remedy = [v for v in got["veto"] if "#506" in v]
    assert len(remedy) == 1
    assert "aaa11111..bbb22222" in remedy[0]
    assert "STILL ON THE BRANCH" in remedy[0]
    assert "git revert --no-commit aaa11111..bbb22222" in remedy[0]
    assert "REMOVE the 1 finding(s) attributed to it (1×P3)" in remedy[0]
    assert "COST the 1 it was sent to answer" in remedy[0] and "1×P1" in remedy[0]
    assert "A PROPOSAL AND NOT AN ACTION" in remedy[0]


def test_the_proposal_is_a_SECOND_line_and_does_not_swallow_the_one_that_fired():
    """Two bullets, read at different moments: #489's says why this round's quiet does
    not count, and #506's is a decision somebody has to take. Folded together, the
    reader deciding what to do next has to re-derive the measurement out of the
    remedy."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(), revert=_armed())
    assert any("escalate_on.fix_injection" in v and "not convergence" in v
               for v in got["veto"])
    assert any("STILL ON THE BRANCH" in v for v in got["veto"])


def test_the_proposal_DECIDES_NOTHING():
    """Every other argument to `round_stop` can move `stop`. This one may not, in
    either direction — a remedy is not a rule — so the verdict is byte-identical with
    and without it, and only the veto list differs."""
    without = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                      injection=_injection())
    with_it = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                      injection=_injection(), revert=_armed())
    assert (without["stop"], without["reason"]) == (with_it["stop"], with_it["reason"])
    assert without["confident"] == with_it["confident"] is False
    assert len(with_it["veto"]) == len(without["veto"]) + 1


def test_a_round_the_gate_did_not_fire_on_proposes_nothing_however_readable_its_range():
    """`offered` is `fix_injection.fired`'s counterpart one rule down, and it has to
    be: a dry, converged round has a perfectly readable fix range and there is nothing
    wrong with the pass in it. Proposing a revert there would be an accusation about a
    cycle that worked."""
    got = panel_rounds.round_stop(2, 5, [], [], [],
                                  injection=_injection(9, 1), revert=_armed())
    assert got["stop"] is True and got["confident"] is True
    assert got["revert"]["offered"] is False
    assert not any("#506" in v for v in got["veto"])


def test_a_below_floor_policy_stop_is_over_the_rate_and_still_proposes_nothing():
    """The commonest round where `over` is true and `fired` is not. #165's floor stops
    are policy stops that are deliberately not vetoed, and a revert proposal attached
    to one would arrive on a confident, converged verdict."""
    quiet = [_finding("P4", key_from=f"nit {i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 5, [c.key for c in quiet], quiet, [],
                                  trigger_floor="P2", fix_floor="P2",
                                  injection=_injection(9, 1), revert=_armed())
    assert got["fix_injection"]["over"] is True
    assert got["fix_injection"]["fired"] is False
    assert got["revert"]["offered"] is False and got["confident"] is True


def test_an_injected_round_that_cannot_name_the_pass_says_SO_rather_than_nothing():
    """The case #500 makes and this must answer plainly. It is not normally reachable
    — a blind round attributes every finding `unknown`, so the rate cannot cross —
    which is exactly why it is a branch and not an assertion: a caller that hands this
    an unreadable range must be told the pass cannot be named, not shown a proposal
    with no range in it, and not shown silence that reads as "nothing to propose"."""
    blind = panel_rounds.revert_state(
        panel_scope.FIX_RANGE_BLIND,
        why="aaa11111..bbb22222 have diverged — the branch was rewritten between rounds")
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(), revert=blind)
    assert got["revert"]["offered"] is False
    said = [v for v in got["veto"] if "#506" in v]
    assert len(said) == 1
    assert "CANNOT BE NAMED" in said[0] and "rewritten between rounds" in said[0]
    assert "#500" in said[0]


def test_an_INCREMENT_round_says_its_cost_column_is_a_ceiling():
    """Under the default scope this round re-read only the fix commit, so a complaint
    in a file it never looked at again is in the cost column beside one the pass
    genuinely cleared. That pushes in the safe direction — a longer cost list argues
    against the revert — and it must not be presented as a measurement."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(),
                                  revert=_armed(scope="increment"))
    remedy = next(v for v in got["veto"] if "#506" in v)
    assert "read it as a ceiling" in remedy


def test_a_WHOLE_PR_round_does_not_claim_a_caveat_that_is_false_of_it():
    """The same round under `--scope pr` really did re-read everything the pass was
    sent to fix, so the ceiling sentence would be untrue — and a caveat printed on
    every round is one the reader learns to skip on the round it matters."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(), revert=_armed(scope="pr"))
    remedy = next(v for v in got["veto"] if "#506" in v)
    assert "ceiling" not in remedy


def test_a_pass_that_cleared_nothing_is_priced_as_costing_nothing():
    """The strongest case for a revert there is, and the sentence has to survive it:
    with an empty cost column the generic "COST the 0 it was sent to answer" reads as
    an arithmetic accident rather than as the finding it is."""
    got = panel_rounds.round_stop(2, 5, ["k1", "k2", "k3", "k4"], [], [],
                                  injection=_injection(), revert=_armed(costs=[]))
    remedy = next(v for v in got["veto"] if "#506" in v)
    assert "COST nothing this round can see" in remedy


def test_what_the_pass_left_outstanding_is_said_because_it_is_free_to_revert():
    """A complaint the pass never cleared is outstanding either way. Left unsaid, a
    reader weighing the cost column has no way to see how much of the pass's brief it
    did not deliver — which is the other half of "was this pass worth keeping"."""
    got = panel_rounds.round_stop(
        2, 5, ["k1", "k2", "k3", "k4"], [], [], injection=_injection(),
        revert=_armed(still_open=[{"key": "k-two", "severity": "P2",
                                   "file": "b.py", "line": 3, "title": "no backoff"}]))
    remedy = next(v for v in got["veto"] if "#506" in v)
    assert "1 of its complaint(s) are still outstanding either way" in remedy


def test_the_proposal_rides_in_the_payload_whether_it_was_offered_or_not():
    """`fix_injection`'s rule and for its reason: an absent key and "there was nothing
    to propose" are different claims, and a consumer forced to tell them apart would be
    reading the payload's age rather than the cycle's state. `kind` is what says which
    of the two it was, in `_fix_range_diff`'s own words."""
    got = panel_rounds.round_stop(2, 5, [], [], [])
    assert got["revert"] == {"kind": "not-asked", "why": None, "base": None,
                             "head": None, "spans": None, "round": None,
                             "range": None, "command": None, "no_command": None,
                             "commits": [], "commit_count": None, "merges": None,
                             "scope": "", "removes": [], "costs": [],
                             "still_open": [], "offered": False}


# --------------------------------------------------------------------- through run()
#
# The unit tests above prove the proposal is built correctly from inputs handed to it.
# What is only coverable here is that `run()` hands it the RIGHT ones — the range
# provenance actually attributed against, and the findings that attribution actually
# placed — because a proposal assembled from a second walk over the same diff could
# accuse a different pass from the one the rate accused, and every test above would
# stay green.

CFG = {
    "github": "acme/e2e",
    "path": "/tmp/acme-e2e",
    "_rules_baseline": ".harness-rules.sample",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
    # Whole-PR scope on purpose: under the default `increment` a round 2 re-reads only
    # the fix commit, and the cost column would then be "what this round did not look
    # at" rather than "what the pass cleared". The scope-dependent caveat has its own
    # test above; here the arithmetic is what is being pinned.
    "review_panel": {"round_scope": "pr"},
}


@pytest.fixture(autouse=True)
def every_seat_is_on_this_box(monkeypatch):
    """Pin the HOST out of every round in this file — `test_panel_provenance` has the
    argument. #138's `seat_ceilings` skips a seat whose CLI is absent, so a test that
    leaves the real predicate in place asserts on which vendor CLIs the machine running
    the suite happens to carry."""
    monkeypatch.setattr(pf, "seat_installed", lambda name: True)

PR_DIFF = ("diff --git a/app/sync.py b/app/sync.py\n"
           "index 1111111..2222222 100644\n"
           "--- a/app/sync.py\n"
           "+++ b/app/sync.py\n"
           "@@ -10,0 +11,2 @@\n"
           "+introduced_by_the_fix()\n"
           "+and_this_one_too()\n")

#: One body answers both compare reads the round makes — `_fix_range_diff` takes
#: `status`/`files` and `fix_pass_commits` takes `total`/`commits`, and the stub is
#: keyed on the path rather than on the `--jq`. A superset therefore serves both, which
#: is also what the real endpoint returns before either expression narrows it.
FIX_COMPARE = json.dumps({
    "status": "ahead",
    "files": [{"filename": "app/sync.py",
               "patch": "@@ -10,0 +11,2 @@\n"
                        "+introduced_by_the_fix()\n+and_this_one_too()"}],
    "total": 1,
    "commits": [{"sha": "bbb222", "title": "fix: close the mirror", "parents": 1}]})

#: The same pass with a base-branch merge inside it — `ahead`, so nothing above this
#: notices, and a wholesale revert of it would undo commits no fix pass wrote.
MERGY_COMPARE = json.dumps({
    "status": "ahead",
    "files": [{"filename": "app/sync.py",
               "patch": "@@ -10,0 +11,2 @@\n"
                        "+introduced_by_the_fix()\n+and_this_one_too()"}],
    "total": 2,
    "commits": [{"sha": "bbb222", "title": "fix: close the mirror", "parents": 1},
                {"sha": "ccc333", "title": "Merge branch 'main' into feat/x",
                 "parents": 2}]})


def _panel_round(monkeypatch, tmp_path, round_no, findings, head, baseline=(),
                 compare=None):
    """One panel run with every subprocess replaced — `test_panel_provenance`'s
    helper, narrowed to what these tests vary."""
    fake_sh = gh_stub(
        meta={"title": "feat: mirror", "additions": 20, "deletions": 2,
              "headRefOid": head},
        compare=FIX_COMPARE if compare is None else compare,
        diff=PR_DIFF)

    def fake_review(name, model, prompt, effort="", **_kw):
        return panel.ReviewerRun(
            [panel.Finding("claude", sev, f, ln, t, "detail")
             for f, ln, t, sev in findings] if name == "claude" else [], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None, ci="",
                        **_kw):
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=f.severity,
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", detail="detail",
                                 reported_by=[f], rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None, "")

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("e2e", 77, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=2) == 0
    return str(out), json.loads(out.read_text())


def test_a_cycle_that_ends_on_injection_ships_a_named_range_and_a_price(
        monkeypatch, tmp_path):
    """The end-to-end shape of #506. Round 1 raises one P1; the fix pass clears it and
    writes three new defects on the lines it added; round 2 finds four things, three of
    them the pass's own damage, and the gate ends the cycle.

    Before this, that was the whole story and the pass stayed on the branch. Now the
    payload names `aaa111..bbb222`, prices the P1 the revert would hand back against
    the three P2s it would remove, and the veto a human reads off the PR comment says
    the change is still there."""
    r1_path, r1 = _panel_round(
        monkeypatch, tmp_path, 1,
        [("app/sync.py", 9, "a stale mirror", "P1")], head="aaa111")
    _, r2 = _panel_round(
        monkeypatch, tmp_path, 2,
        [("app/sync.py", 11, "the fix left a dangling handle", "P2"),
         ("app/sync.py", 12, "and dropped the lock with it", "P2"),
         ("app/sync.py", 11, "and never closed the socket", "P2"),
         ("app/sync.py", 90, "an unrelated defect nobody saw", "P2")],
        head="bbb222", baseline=[r1_path])

    rv = r2["round_stop"]["revert"]
    assert r2["round_stop"]["fix_injection"]["fired"] is True
    assert rv["offered"] is True
    # The SAME range provenance attributed against — round 1's head to round 2's — so
    # the proposal cannot point at a different pass from the one the rate accused.
    assert (rv["base"], rv["head"]) == (r1["head_sha"], r2["head_sha"])
    assert rv["range"] == "aaa111..bbb222" and rv["round"] == 1
    assert rv["command"] == "git revert --no-commit aaa111..bbb222"
    assert rv["merges"] == 0 and rv["commit_count"] == 1
    assert [c["title"] for c in rv["commits"]] == ["fix: close the mirror"]
    # What it would remove: the findings this round's own provenance placed in
    # `introduced`, and not a re-derivation of them.
    assert len(rv["removes"]) == r2["provenance_counts"]["introduced"] == 3
    assert all(r["severity"] == "P2" for r in rv["removes"])
    # ...against what it would cost: the P1 the pass was sent to and cleared.
    assert [(r["key"], r["severity"]) for r in rv["costs"]] == \
        [(r1["to_fix"][0]["key"], "P1")]
    assert rv["still_open"] == []
    # And the human half, on the PR comment, where the cycle's own stop cannot say it.
    assert any("STILL ON THE BRANCH" in v and "aaa111..bbb222" in v
               for v in r2["round_stop"]["veto"])
    assert any("#506" in n and "does not remove it from the branch" in n
               for n in r2["config_notes"])


def test_a_pass_that_did_not_clear_its_own_brief_is_not_priced_as_if_it_had(
        monkeypatch, tmp_path):
    """The same cycle with the round-1 P1 still outstanding in round 2. It is
    outstanding whether or not anyone reverts, so it belongs in `still_open` and NOT
    in the cost column — priced as a cost it would argue against a revert with a
    finding the revert cannot lose."""
    r1_path, r1 = _panel_round(
        monkeypatch, tmp_path, 1,
        [("app/sync.py", 9, "a stale mirror", "P1")], head="aaa111")
    _, r2 = _panel_round(
        monkeypatch, tmp_path, 2,
        [("app/sync.py", 9, "a stale mirror", "P1"),
         ("app/sync.py", 11, "the fix left a dangling handle", "P2"),
         ("app/sync.py", 12, "and dropped the lock with it", "P2"),
         ("app/sync.py", 11, "and never closed the socket", "P2"),
         ("app/sync.py", 90, "an unrelated defect nobody saw", "P2")],
        head="bbb222", baseline=[r1_path])

    rv = r2["round_stop"]["revert"]
    assert rv["costs"] == []
    assert [r["key"] for r in rv["still_open"]] == [r1["to_fix"][0]["key"]]


def test_a_REBASE_leaves_the_offending_pass_unnameable_and_the_round_says_both(
        monkeypatch, tmp_path):
    """#500's case, read through #506. A rewrite between rounds removes the range, so
    every finding is `unknown`, the gate cannot fire — and neither can this, because
    the range that would identify the offending pass is the range that is missing.

    The requirement is that it says so rather than guessing or going quiet. #500's own
    veto is where it is said, in #500's words, because a second sentence about the same
    missing range would be the same news twice."""
    r1_path, _ = _panel_round(
        monkeypatch, tmp_path, 1,
        [("app/sync.py", 9, "a stale mirror", "P1")], head="aaa111")
    _, r2 = _panel_round(
        monkeypatch, tmp_path, 2,
        [("app/sync.py", 11, "the fix left a dangling handle", "P2"),
         ("app/sync.py", 12, "and dropped the lock with it", "P2"),
         ("app/sync.py", 11, "and never closed the socket", "P2")],
        head="bbb222", baseline=[r1_path],
        compare=json.dumps({"status": "diverged", "files": []}))

    # FIRST, because it is the assertion that has pre-fix behaviour to fail against:
    # #500's veto line already existed and said nothing about a pass nobody can name.
    assert any("#506" in v and "NAME the offending pass" in v
               for v in r2["round_stop"]["veto"])
    rv = r2["round_stop"]["revert"]
    # `rewritten`, not `blind`, and the difference arrived in the merge rather than
    # here: this test was written against a three-value vocabulary and `main` has
    # since split the rebase out of `blind` into a verdict of its own (#512). The
    # assertion moved to the narrower value instead of being loosened to accept
    # either, because `rewritten` is the whole of what this test is about — a
    # `blind` range that still attributed would not leave the pass unnameable.
    assert rv["kind"] == panel_scope.FIX_RANGE_REWRITTEN
    assert rv["range"] is None and rv["command"] is None
    assert rv["offered"] is False and r2["round_stop"]["fix_injection"]["over"] is False
    # The cost column survives the blindness, because it does not come from the range.
    assert len(rv["costs"]) == 1


def test_a_fix_pass_carrying_a_base_branch_MERGE_is_named_but_not_handed_a_command(
        monkeypatch, tmp_path):
    """The end-to-end half of Codex's finding, and the case it is easy to miss: the
    compare still reads `ahead`, so nothing upstream of this notices, and the range
    holds a merge of `main` alongside the fixer's own commit. The proposal is still
    made — the range is named and both columns are priced — and the command is not."""
    r1_path, _ = _panel_round(
        monkeypatch, tmp_path, 1,
        [("app/sync.py", 9, "a stale mirror", "P1")], head="aaa111")
    _, r2 = _panel_round(
        monkeypatch, tmp_path, 2,
        [("app/sync.py", 11, "the fix left a dangling handle", "P2"),
         ("app/sync.py", 12, "and dropped the lock with it", "P2"),
         ("app/sync.py", 11, "and never closed the socket", "P2"),
         ("app/sync.py", 90, "an unrelated defect nobody saw", "P2")],
        head="bbb222", baseline=[r1_path], compare=MERGY_COMPARE)

    rv = r2["round_stop"]["revert"]
    assert rv["offered"] is True and rv["range"] == "aaa111..bbb222"
    assert rv["merges"] == 1 and rv["command"] is None
    assert "merge commit" in rv["no_command"]
    assert not any("git revert --no-commit" in v for v in r2["round_stop"]["veto"])


def test_round_one_records_a_proposal_that_was_never_asked_for(monkeypatch, tmp_path):
    """There is no earlier round, so there is no pass between two rounds. The payload
    still carries the key — the alternative is a consumer telling "nothing to propose"
    from "this harness is too old to propose" by the absence of a field."""
    _, r1 = _panel_round(monkeypatch, tmp_path, 1,
                         [("app/sync.py", 9, "a stale mirror", "P1")], head="aaa111")
    assert r1["round_stop"]["revert"]["kind"] == "not-asked"
    assert r1["round_stop"]["revert"]["offered"] is False
