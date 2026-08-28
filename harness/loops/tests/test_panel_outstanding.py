"""#42: what a cycle leaves behind, and who gets it.

`round_stop` answers one question — *should another panel run?* — and until this
block existed `panel-review-pr.md` §5 read it as answering a second one it was never
computed from: *should these findings be fixed?* By that command's own bar the second
answer is always yes, for every confirmed finding and every SonarCloud hard-gate
issue, so the two come apart exactly where a stop is a COST bound rather than a
convergence. That is the cap, and the cap is the common case: `stop` flipped true with
`"round cap (N) reached — …, unreviewed"`, §5 launched a fixer only on `stop: false`,
and the final round's P1/P2s, its repeats whose fix did not land, its gate issues and
everything it newly found were found, judged, posted to the PR, recorded on the board,
and **handed to nobody**.

Four claims are pinned here:

* the MEASUREMENT — `fixable` / `below_floor` / `escalated`, computed off ONE
  universe so that a key reaching only one of `round_stop`'s three work parameters
  cannot be counted for the stop and dropped from the disposal, which is this bug one
  level down;
* the VERDICT — `handed_to`, kept apart from the lists on exactly the terms
  `fix_injection` keeps `fired` apart from `over`, and therefore null on a round that
  is going again rather than answering for a final pass nobody is running;
* the ROUTING — a cap's remainder goes to a fixer and a futility rung's goes to a
  HUMAN, because every one of those rungs says so in its own `reason` and a payload
  must not contradict a sentence it is carrying;
* the HONEST END STATE — a cycle ending with clearable work left ends with either
  unfixed findings or an unreviewed fix, there is no third option, and `why` says
  which one the default takes.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_preflight  # noqa: E402
import panel_rounds  # noqa: E402
from conftest import gh_stub  # noqa: E402


def _finding(severity="P2", title="boom", file="a.py", line=1, verdict="confirmed"):
    reported = [panel.Finding("claude", severity, file, line, title, "")]
    return panel.Canonical(id=f"42-{title}", severity=severity, file=file, line=line,
                           synthesis=title, verdict=verdict, reported_by=reported)


def _flat(series=(9, 9), limit=1):
    return panel_rounds.not_falling_state(
        [(1 + i, n) for i, n in enumerate(series)], limit)


def _injected(introduced=3, missed=1, limit=0.5):
    return panel_rounds.injection_state(
        {"introduced": introduced, "missed": missed, "unread": 0, "unknown": 0}, limit)


# --------------------------------------------------------------- the measurement

def test_a_capped_round_hands_its_findings_to_a_final_fix_pass():
    """The bug, in one assertion. Round 2 of 2 with a P1 outstanding: `stop` is true,
    the reason says the cap was reached, and until #42 that was the whole payload —
    the P1 was posted to the PR and handed to nobody."""
    got = panel_rounds.round_stop(2, 2, [], [_finding("P1", "unsafe cast")], [])
    assert got["stop"] is True
    assert "round cap (2) reached" in got["reason"]
    assert got["outstanding"]["handed_to"] == "fixer"
    assert len(got["outstanding"]["fixable"]) == 1


def test_the_findings_are_named_and_not_merely_counted():
    """A count cannot be turned into a fixer's brief. The keys are what §5 hands on,
    and they are the round's own keys so the brief and the PR comment agree."""
    findings = [_finding("P1", "unsafe cast"), _finding("P2", "no timeout")]
    got = panel_rounds.round_stop(2, 2, [], findings, [])
    assert got["outstanding"]["fixable"] == sorted(c.key for c in findings)


def test_a_sonar_gate_issue_is_fixable_however_low_its_severity():
    """Sonar's own severities are routinely P3/P4 and a red gate is not a judged
    opinion about severity — it is a merge gate, exempt from both floors at every
    rule. A disposal that filtered by severity would drop the one class of finding
    that keeps the PR unmergeable."""
    gate = _finding("P4", "python:S1481", verdict="sonar")
    got = panel_rounds.round_stop(2, 2, [], [gate], [], fix_floor="P2")
    assert got["outstanding"]["fixable"] == [gate.key]
    assert got["outstanding"]["below_floor"] == []


def test_one_universe_so_a_key_cannot_fall_between_the_parameters():
    """Rules 2 and 3 read `outstanding` and `repeated`, rule 1 reads `new_keys`. A key
    that reaches only one of them was counted for the STOP and dropped from the
    disposal — which is this same bug one level down, and the reason the lists are
    built off the union rather than off `outstanding` alone."""
    got = panel_rounds.round_stop(2, 2, ["only-new"], [], [],
                                  repeated=["only-repeated"])
    assert got["outstanding"]["fixable"] == ["only-new", "only-repeated"]
    assert got["outstanding"]["handed_to"] == "fixer"


def test_a_below_floor_finding_is_listed_and_is_handed_to_nobody():
    """#165's stop is a POLICY stop: the repo said these are reported and not fixed
    here, so handing them to a fixer would re-open a decision it has already taken.
    They are still NAMED — silence about them is what lets such a stop read as a dry
    one, and a reader cannot tell "nothing was found" from "four were found and the
    floor held them back" if the payload says the same thing for both."""
    got = panel_rounds.round_stop(2, 5, [], [_finding("P4", "a nit")], [],
                                  fix_floor="P2", trigger_floor="P2")
    assert got["stop"] is True
    assert got["outstanding"]["handed_to"] == "nobody"
    assert got["outstanding"]["fixable"] == []
    assert len(got["outstanding"]["below_floor"]) == 1
    assert "P2 fix floor" in got["outstanding"]["why"]


def test_a_dry_stop_says_there_is_nothing_rather_than_saying_nothing():
    got = panel_rounds.round_stop(2, 5, [], [], [])
    assert got["stop"] is True
    assert got["outstanding"] == {"fixable": [], "below_floor": [], "escalated": [],
                                 "handed_to": "nobody",
                                 "why": "nothing is outstanding — the cycle ends "
                                        "with nothing to hand on"}


# -------------------------------------------------------------------- the verdict

def test_a_round_going_again_makes_no_disposal():
    """`fix_injection`'s `over`/`fired` split, applied once more. The lists are a
    property of the ROUND and are true of it either way; `handed_to` is the property
    of the VERDICT. A caller gating a FINAL, unreviewed fix pass on that field must
    not have it answered by a round that is mid-cycle and will be reviewed."""
    got = panel_rounds.round_stop(1, 5, [], [_finding("P1", "unsafe cast")], [])
    assert got["stop"] is False
    assert got["outstanding"]["handed_to"] is None
    assert got["outstanding"]["why"] is None
    # ...and the measurement is still there, because it is true of this round.
    assert len(got["outstanding"]["fixable"]) == 1


def test_the_escalated_list_cannot_disagree_with_the_sibling_key():
    """`escalated` is `escalated_outstanding` under a second name, off the same local.
    Repeated in the block because this block is the whole answer to "who gets what is
    left", and a reader who has to join it against a sibling key to find the one class
    of finding no fixer may take is a reader who will not."""
    held = _finding("P1", "the approach is wrong")
    got = panel_rounds.round_stop(2, 2, [], [held], [], escalated=[held.key])
    assert got["outstanding"]["escalated"] == got["escalated_outstanding"] == [held.key]


def test_an_escalated_finding_is_never_a_fixers_and_goes_to_a_human():
    """#221: no fix round may touch an escalated finding, at any stop. Putting one in
    `fixable` would hand a fixer the exact work the escalation exists to withhold."""
    held = _finding("P1", "the approach is wrong")
    got = panel_rounds.round_stop(2, 2, [], [held], [], escalated=[held.key])
    assert got["outstanding"]["fixable"] == []
    assert got["outstanding"]["handed_to"] == "human"
    assert "a human answers the premise" in got["outstanding"]["why"]


def test_an_escalation_beside_real_work_still_sends_the_real_work_to_a_fixer():
    """The mixed case, which is why the escalation is a filter rather than a stop. The
    P2 is clearable and goes to the final pass; the escalated finding stays listed for
    a human, and §6 relays it separately."""
    held = _finding("P1", "the approach is wrong")
    real = _finding("P2", "no timeout")
    got = panel_rounds.round_stop(2, 2, [], [held, real], [], escalated=[held.key])
    assert got["outstanding"]["fixable"] == [real.key]
    assert got["outstanding"]["escalated"] == [held.key]
    assert got["outstanding"]["handed_to"] == "fixer"
    # And `why` says so itself. It is the sentence the relay repeats, so a reader
    # acting on it alone must not send the escalation to the fix pass with the rest.
    assert "escalated finding(s) are outstanding beside them" in got["outstanding"]["why"]
    assert "NOT a fixer's" in got["outstanding"]["why"]


# -------------------------------------------------------------------- the routing

@pytest.mark.parametrize("kwargs, rung", [
    ({"not_falling": _flat()}, "new_findings_not_falling"),
    ({"injection": _injected()}, "fix_injection"),
])
def test_a_futility_rungs_remainder_goes_to_a_human_and_not_to_a_fixer(kwargs, rung):
    """Each of these rungs ends the cycle by saying, in its own `reason`, that a human
    answers this rather than another fix pass. Sending their remainder to a fixer would
    contradict a sentence the same payload is carrying — and that is the distinction
    the cap does not have, which is why the cap is the rule #42 was written for."""
    findings = [_finding("P2", f"f{i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 5, [c.key for c in findings], findings, [],
                                  **kwargs)
    assert got["stop"] is True
    assert got[rung]["fired"] is True
    assert "not another fix pass" in got["reason"] or "a human triages" in got["reason"]
    assert got["outstanding"]["handed_to"] == "human"
    assert "Triage what is left" in got["outstanding"]["why"]
    # The work itself is still named — routed differently, never dropped.
    assert got["outstanding"]["fixable"] == sorted(c.key for c in findings)


def test_a_premise_declared_twice_sends_its_remainder_to_a_human():
    """#84's brake, on the same rule: the answer to a repeated premise was never
    another fix pass."""
    reg = {"premises": [{"key": "k", "text": "the mirror is idempotent",
                         "rounds": [1, 2], "decidable": "yes"}]}
    got = panel_rounds.round_stop(
        2, 5, [], [_finding("P2", "no timeout")], [],
        premises=panel_rounds.premise_state(reg, 2, 1))
    assert got["stop"] is True
    assert got["outstanding"]["handed_to"] == "human"


def test_the_cap_is_a_cost_bound_and_does_not_route_like_a_futility_rung():
    """The two stops are not the same claim and must not have the same disposal. A cap
    says the cycle has spent enough; it says nothing about what the next fix pass would
    be worth, and a fix pass is exactly what its remainder needs."""
    findings = [_finding("P2", f"f{i}") for i in range(4)]
    capped = panel_rounds.round_stop(2, 2, [c.key for c in findings], findings, [])
    futile = panel_rounds.round_stop(2, 5, [c.key for c in findings], findings, [],
                                     not_falling=_flat())
    assert capped["stop"] is futile["stop"] is True
    assert capped["outstanding"]["fixable"] == futile["outstanding"]["fixable"]
    assert capped["outstanding"]["handed_to"] == "fixer"
    assert futile["outstanding"]["handed_to"] == "human"


# ----------------------------------------------------------- the honest end state

def test_the_default_says_out_loud_that_the_commit_ships_unreviewed():
    """The end state #42 insists on stating rather than hiding: the cycle ends with
    either unfixed findings or an unreviewed fix, there is no third option, and the
    workflow used to take the first in silence. `why` is the sentence §5 makes the
    relay repeat."""
    why = panel_rounds.round_stop(
        2, 2, [], [_finding("P1", "unsafe cast")], [])["outstanding"]["why"]
    assert "UNREVIEWED" in why
    assert "no third option" in why


def test_it_proposes_and_does_not_order():
    """#506's constraint, inherited. Nothing here runs a fixer, and a cycle that ends
    with findings the user would rather triage than patch is the user's to end that
    way."""
    why = panel_rounds.round_stop(
        2, 2, [], [_finding("P1", "unsafe cast")], [])["outstanding"]["why"]
    assert "A PROPOSAL AND NOT AN ACTION" in why
    assert "the choice is the operator's" in why


def test_the_disposal_moves_no_verdict():
    """It reports and decides nothing: adding the block must not have changed which
    rounds stop, why, or whether the stop was earned. `revert` is the precedent —
    the only other argument to this function that decides nothing."""
    findings = [_finding("P2", f"f{i}") for i in range(4)]
    got = panel_rounds.round_stop(2, 5, [c.key for c in findings], findings, [])
    assert got["stop"] is False and got["confident"] is False
    assert got["reason"] == "4 finding(s) no earlier round raised"
    assert got["veto"] == []


# --------------------------------------------------------------------- the report

E2E_CFG = {
    "github": "acme/e2e",
    "path": "/nonexistent/acme-e2e",
    "_rules_baseline": ".harness-rules.sample",
    "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
    "review_panel": {"refuse_over_cap_multiple": 0, "manifest_moves": False},
}

PR_DIFF = ("diff --git a/app/sync.py b/app/sync.py\n"
           "--- a/app/sync.py\n"
           "+++ b/app/sync.py\n"
           "@@ -1,3 +1,4 @@\n"
           " def sync():\n"
           "+    mirror()\n"
           "     return 1\n")


@pytest.fixture
def every_seat_is_on_this_box(monkeypatch):
    monkeypatch.setattr(panel_preflight, "seat_installed", lambda name: True)


def _capped_round(monkeypatch, tmp_path):
    """One whole round that is also the last one — `max_rounds=1`, so rule 1 fires and
    the cap converts it into a stop with the finding still outstanding."""
    fake_sh = gh_stub(meta={"title": "feat: mirror", "additions": 20, "deletions": 2,
                            "headRefOid": "a" * 40}, diff=PR_DIFF)

    def fake_review(name, model, prompt, effort="", **_kw):
        return panel.ReviewerRun(
            [panel.Finding("claude", "P2", "app/sync.py", 2, "mirror is called twice",
                           "detail")], None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None,
                        ci="", recurrence="", **_kw):
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity="P2",
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", detail="detail",
                                 reported_by=[f], rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None,
                panel.CoverageRuling())

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: E2E_CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / "capped.json"
    assert panel.run("e2e", 42, post=False, json_file=str(out), record=False,
                     round_no=1, max_rounds=1) == 0
    return json.loads(out.read_text())


def test_the_pr_comment_says_what_is_left_and_who_has_it(
        monkeypatch, tmp_path, capsys, every_seat_is_on_this_box):
    """The PR comment is where a human reads the verdict, and a payload field nobody
    renders is a field nobody acts on. The line is UNDER the veto lines for #507's
    reason: a reader meets what ended the cycle before what is still owed because of
    it."""
    payload = _capped_round(monkeypatch, tmp_path)
    assert payload["round_stop"]["outstanding"]["handed_to"] == "fixer"
    out = capsys.readouterr().out
    assert "**Outstanding — handed to a final fix pass:**" in out
    assert "UNREVIEWED" in out
    # The keys themselves, so the brief for that pass can be built off the comment.
    key = payload["round_stop"]["outstanding"]["fixable"][0]
    assert f"`{key}`" in out


def test_the_comment_is_silent_on_a_round_that_is_going_again(
        monkeypatch, tmp_path, capsys, every_seat_is_on_this_box):
    """`handed_to` is null mid-cycle, and a line here would announce a final pass
    nobody is running — §5's ordinary path is taking these findings to the next fix."""
    fake_sh = gh_stub(meta={"title": "feat: mirror", "additions": 20, "deletions": 2,
                            "headRefOid": "a" * 40}, diff=PR_DIFF)

    def fake_review(name, model, prompt, effort="", **_kw):
        return panel.ReviewerRun(
            [panel.Finding("claude", "P2", "app/sync.py", 2, "mirror twice", "d")],
            None, 800, None)

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None,
                        ci="", recurrence="", **_kw):
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity="P2",
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", detail="d",
                                 reported_by=[f], rationale="real")
                 for i, grp in enumerate(clusters) for f in grp], None,
                panel.CoverageRuling())

    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: E2E_CFG)
    monkeypatch.setattr(panel_core, "sh", fake_sh)
    monkeypatch.setattr(panel, "review_llm", fake_review)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out_file = tmp_path / "going-again.json"
    assert panel.run("e2e", 42, post=False, json_file=str(out_file), record=False,
                     round_no=1, max_rounds=3) == 0
    payload = json.loads(out_file.read_text())
    assert payload["round_stop"]["stop"] is False
    assert payload["round_stop"]["outstanding"]["handed_to"] is None
    assert "Outstanding — handed to" not in capsys.readouterr().out
