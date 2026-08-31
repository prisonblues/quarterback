"""#615's fourth outcome: the fix pass answered the finding, and only narrowly.

The fixer's vocabulary was `fixed | refuted | deferred` plus escalation, and every
one of those answers *whether* to act. None of them answers *how far*. A finding
names a symptom at a line; the general form of that symptom is a class; and every
instruction on `review-pr.md` — "fix everything you find", "never note a problem and
move on" — points at the class. So a fixer with no way to answer a finding partially
answers it maximally, and the maximal answer is what makes a pass edit files the
finding never named. On `prisonblues/lexray#1780` one finding about one route became
server-level nginx `gzip`, and the round after that was a P1.

`narrowed` is the name for the partial answer, and the whole of its design is that it
**CLEARS**: the finding was fixed at the point it was raised, so the round does not go
again for it, it is not outstanding, it takes no veto line and it costs the round no
confidence. That is what separates it from an escalation, which stops a finding
counting because a human owes an answer on it.

Two rules bound the word, and both are here because both are what a fixer could
otherwise buy with it: a SonarCloud hard-gate issue cannot be narrowed away, and a key
naming nothing this round raised is reported rather than silently honoured.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/tmp/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}

#: A key of the right shape that names nothing — 16 hex characters, which is what
#: `_is_key` accepts and what a finding's own key looks like.
STRANGER = "a0b1c2d3e4f56789"


def judged(severity, title="unvalidated input", file="a.py"):
    """A judge-confirmed finding, whose `.key` the test then reads. The key is derived
    from the file and the reporters' words, so a test cannot hand one in: two findings
    differ in their keys by differing in what they are ABOUT, which is also the only
    way they differ in production."""
    reports = [panel.Finding("claude", severity, file, 3, title, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=3,
                           synthesis=title, verdict="confirmed", reported_by=reports,
                           rationale="real")


def sonar_canonical(severity, title="unused import", file="b.py"):
    """A SonarCloud hard-gate issue as `panel.py` builds it for `outstanding`: Sonar's
    OWN severity — routinely P3/P4 — and `verdict="sonar"`, the field `round_stop`
    identifies a gate issue by."""
    reports = [panel.Finding("sonarqube", severity, file, 7, title, "python:S1128")]
    return panel.Canonical(id="34-F02", severity=severity, file=file, line=7,
                           synthesis=title, verdict="sonar", reported_by=reports,
                           rationale="python:S1128")


# ---------------------------------------------------------------- it CLEARS

def test_a_narrowed_finding_does_not_buy_another_round():
    """The whole of the feature in one round. Without the declaration this P2 is
    outstanding, rule 2 fires, and the cycle goes again for a finding the fix pass has
    already answered as far as it is going to."""
    c = judged("P2")
    assert panel.round_stop(2, 5, [], [c], [], repeated=[c.key])["stop"] is False
    d = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], narrowed=[c.key])
    assert d["stop"] is True
    # And it says WHY, in its own words. Not "dry": see the section on the reason
    # below, which is the whole of what this stop may not borrow.
    assert "answered at the point they were raised" in d["reason"]
    assert "dry" not in d["reason"]


def test_it_clears_rather_than_merely_stopping_the_finding_counting():
    """The difference from an escalation, which is the one misreading this flag can
    produce. An escalation is an open question, so it takes a veto line and the round
    is not confident; a narrowing is an answer already given, so the round is reported
    as what it is — a clean stop with nothing left to hand on."""
    c = judged("P2")
    d = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], narrowed=[c.key])
    assert d["veto"] == []
    assert d["confident"] is True and d["converged"] is True
    assert d["outstanding"]["handed_to"] == "nobody"


def test_a_narrowed_finding_is_in_neither_outstanding_list():
    """It is not work a fixer may take and it is not policy-held either, so it is
    absent from `fixable` and from `below_floor` both. That absence is exactly why it
    has to be REPORTED somewhere, which is the test below."""
    c = judged("P2")
    d = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], narrowed=[c.key],
                         cleared_floor="P4", trigger_floor="P2")
    assert d["outstanding"]["fixable"] == []
    assert d["outstanding"]["below_floor"] == []
    assert d["escalated_outstanding"] == []


def test_the_payload_says_which_findings_were_narrowed_and_says_it_twice():
    """A narrowed finding is in none of the three disposal lists because it was
    ANSWERED, so a reader joining this round's findings against the disposal would
    find it nowhere and read the gap as a dropped finding. It is named at the top
    level and inside the disposal block for the reason `escalated` is repeated there:
    that block is the whole answer to "who gets what is left", and a reader who has to
    join it against a sibling key to find a class of finding is a reader who will
    not."""
    c = judged("P2")
    d = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], narrowed=[c.key])
    assert d["narrowed"] == [c.key]
    assert d["outstanding"]["narrowed"] == [c.key]


def test_rule_1_does_not_fire_on_a_narrowed_finding_raised_again_as_NEW():
    """A fresh panel can re-derive the same defect under the same key and report it as
    new — the filters run once, in front of all four rules, so rule 1 sees the
    subtracted set exactly as rules 2 and 3 do."""
    c = judged("P2")
    d = panel.round_stop(2, 5, [c.key], [c], [], narrowed=[c.key])
    assert d["stop"] is True
    assert d["new_below_trigger_floor"] == []


def test_real_work_beside_a_narrowing_still_earns_another_round():
    """The mixed case, which a "stop on any narrowing" rule would get wrong: one
    narrowed finding beside a live P2 must not end the cycle, or the fix the same pass
    made goes unreviewed."""
    answered = judged("P2")
    live = judged("P2", "a different defect", file="b.py")
    assert answered.key != live.key
    d = panel.round_stop(2, 5, [], [answered, live], [], narrowed=[answered.key])
    assert d["stop"] is False and "P1/P2" in d["reason"]


def test_the_register_reported_is_the_one_this_ROUND_raised():
    """`escalated_outstanding`'s rule, one outcome across. What was subtracted is a
    property of the round; a payload that reported every key the caller named would go
    on crediting a narrowing long after the code moved and the finding was gone."""
    c = judged("P2")
    d = panel.round_stop(2, 5, [], [c], [], narrowed=[c.key, STRANGER])
    assert d["narrowed"] == [c.key]


# ------------------------------------------- the word the stop may NOT borrow

def test_a_narrowing_never_lends_the_round_the_word_dry():
    """The one thing a narrowing may not buy, and the reason it is a bug rather than a
    wording preference. `--narrowed` is passed on the round AFTER the pass that
    declared it, and only keys this round raised are honoured — so every narrowing
    honoured here is a finding a fresh panel put up again after a fixer said it had
    answered it. Reported as "dry — nothing raised that an earlier round had not",
    that is the payload asserting the opposite of what happened, in the round where it
    matters most: a judge-confirmed P1, raised a second time, cleared on the fixer's
    own say-so. `quiet_repeats` earns its own reason for exactly this reason one rule
    up, and a narrowing had none."""
    c = judged("P1")
    d = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], narrowed=[c.key],
                         trigger_floor="P2", cleared_floor="P4")
    assert d["stop"] is True
    assert "dry" not in d["reason"]
    assert "answered at the point they were raised" in d["reason"]
    assert "general form declined" in d["reason"]


def test_the_reason_counts_the_narrowings_at_or_above_the_trigger_floor():
    """A P1 answered narrowly and a P4 answered narrowly are the same mechanism and
    not the same news, and the reason is the only place a reader of the PR comment
    meets either. So the count is said out loud — and it is the TRIGGER floor, the
    dial that decides what is worth another round, because that is the question a
    narrowing answered in the negative."""
    loud, quiet = judged("P1"), judged("P4", "a nit", file="b.py")
    d = panel.round_stop(2, 5, [], [loud, quiet], [],
                         repeated=[loud.key, quiet.key],
                         narrowed=[loud.key, quiet.key],
                         trigger_floor="P2", cleared_floor="P4")
    assert d["stop"] is True
    assert "2 finding(s) were answered" in d["reason"]
    assert "1 of them at or above the P2 round trigger floor" in d["reason"]
    # And the clause is absent rather than reading "0 of them" when nothing was
    # above the floor: a count of zero in a sentence about severity invites the
    # reader to look for the finding it is counting.
    only_quiet = panel.round_stop(2, 5, [], [quiet], [], repeated=[quiet.key],
                                  narrowed=[quiet.key], trigger_floor="P2",
                                  cleared_floor="P4")
    assert "round trigger floor" not in only_quiet["reason"]


def test_a_narrowed_P1_still_keeps_its_confidence_and_still_converges():
    """The decision, pinned so that the next reader knows it was taken rather than
    missed (review of #631). A narrowing at any severity costs no veto line, no
    `confident` and no `converged` — the asymmetry with `escalated`, which costs all
    three, is the point: an escalation names work nobody has done and a human owes an
    answer on, a narrowing names work that was done and bounded. Charging it would
    leave a fixer's only clean way out of a cycle the class-wide fix, which is the
    pressure #615 exists to remove. What it costs instead is the reason line above,
    two lines of justification and a board row — a bill the caller collects."""
    c = judged("P1")
    d = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], narrowed=[c.key],
                         trigger_floor="P2", cleared_floor="P4")
    assert d["veto"] == []
    assert d["confident"] is True and d["converged"] is True
    assert d["outstanding"]["handed_to"] == "nobody"
    # The discriminator: the SAME P1, escalated instead of narrowed, pays all three.
    e = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], escalated=[c.key],
                         trigger_floor="P2", cleared_floor="P4")
    assert e["veto"] and e["confident"] is False and e["converged"] is False


def test_an_escalation_beside_a_narrowing_still_owns_the_reason():
    """The chain's own rule — the most specific TRUE thing wins — applied where the
    two outcomes meet. An open question a human owes an answer on says more about why
    a cycle ended than an answer a fixer has already given, so the escalation keeps
    the `reason` and the veto it always took, and the narrowing is reported in
    `round_stop.narrowed` where it always was."""
    held = judged("P1")
    answered = judged("P2", "a different defect", file="b.py")
    d = panel.round_stop(2, 5, [], [held, answered], [],
                         repeated=[held.key, answered.key],
                         escalated=[held.key], narrowed=[answered.key],
                         trigger_floor="P2", cleared_floor="P4")
    assert d["stop"] is True
    assert "escalated finding(s) await a human" in d["reason"]
    assert d["narrowed"] == [answered.key]
    assert d["confident"] is False


def test_a_below_floor_stop_beside_a_narrowing_keeps_its_own_reason():
    """The other side of the ordering, and the reason it is safe: both floor stops
    say "reported, not fixed here", which is a true statement about work that is still
    open and is not the word this fix is about. Nothing falls through to "dry"."""
    quiet = judged("P4", "a nit", file="b.py")
    answered = judged("P2")
    d = panel.round_stop(2, 5, [quiet.key], [quiet, answered], [],
                         repeated=[answered.key], narrowed=[answered.key],
                         trigger_floor="P2", cleared_floor="P4")
    assert d["stop"] is True
    assert "none at or above the P2 round trigger floor" in d["reason"]
    assert "dry" not in d["reason"]
    assert d["narrowed"] == [answered.key]


# -------------------------------- what a fixer may NOT buy with the word

def test_a_sonar_hard_gate_issue_cannot_be_narrowed_away():
    """The one guarantee this function makes about the word, and it is here rather
    than in a rule because the exemption is a property of the KEY. Narrowing is a
    judgement about how far to fix a JUDGED finding; a red quality gate is not a
    judgement, and it keeps the PR unmergeable at any severity. Honouring the
    declaration would end the cycle confident with the gate still red — the exact bug
    `outstanding = to_fix + sonar` was written to fix, arriving through a third
    door."""
    gate = sonar_canonical("P3")
    d = panel.round_stop(2, 5, [], [gate], [], repeated=[gate.key],
                         narrowed=[gate.key])
    assert d["stop"] is False, d["reason"]
    assert "SonarCloud gate issue" in d["reason"]
    assert d["narrowed"] == []


def test_the_gate_issue_is_subtracted_and_the_judged_finding_beside_it_is_not():
    """The discriminator, so the rule above is testing "a gate issue is exempt" and
    not "a narrowing does nothing". Same round, same declaration, two keys."""
    gate = sonar_canonical("P4")
    c = judged("P4", "a nit")
    d = panel.round_stop(2, 5, [], [gate, c], [], repeated=[gate.key, c.key],
                         narrowed=[gate.key, c.key], cleared_floor="P4")
    assert d["narrowed"] == [c.key]
    assert d["stop"] is False and "SonarCloud gate issue" in d["reason"]


# ---------------------------------------------------- the shape of the argument

@pytest.mark.parametrize("bad", [1, 0])
def test_a_count_is_refused_rather_than_left_to_a_TypeError(bad):
    """`narrowed` is checked at the same door `escalated` and `repeated` are, and for
    the same reason: the subtraction happens by KEY, and a count computed by the
    caller cannot express it."""
    c = judged("P2")
    with pytest.raises(TypeError) as refused:
        panel.round_stop(2, 5, [], [c], [], narrowed=bad)
    assert "not a count" in str(refused.value)


def test_a_bare_string_of_keys_is_refused_rather_than_iterated():
    """A `str` is itself iterable, so `narrowed=key` instead of `narrowed=[key]` — the
    natural slip — would make the register a set of single characters, match no
    finding, and leave the cycle going again with nothing saying why."""
    c = judged("P2")
    with pytest.raises(TypeError) as refused:
        panel.round_stop(2, 5, [], [c], [], narrowed=c.key)
    assert "not one string" in str(refused.value)


def test_an_empty_declaration_changes_nothing():
    """`--narrowed ''` and no flag at all are the same round. Guarded because the
    filter is a membership test, and an empty string in the set would quietly match a
    finding whose key failed to serialise."""
    c = judged("P2")
    plain = panel.round_stop(2, 5, [], [c], [], repeated=[c.key])
    empty = panel.round_stop(2, 5, [], [c], [], repeated=[c.key],
                             narrowed=["", None])
    assert plain["stop"] == empty["stop"] is False
    assert empty["narrowed"] == []


# ------------------------------------------------------------- through `run()`

def _stub(monkeypatch, findings, *, config=None):
    """Every process a round would spawn, replaced."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: config or CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "h", "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n",
        compare='{"status": "ahead", "files": [{"filename": "a.py", "patch": "@@"}]}'))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun(list(findings), None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))

    def adjudicate(clusters, diff, model, pr, budget=None, coverage=None, **_kw):
        flat = [f for grp in clusters for f in grp]
        return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=f.severity,
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", detail=f.detail,
                                 reported_by=[f], rationale="real")
                 for i, f in enumerate(flat)], None, panel.CoverageRuling())

    monkeypatch.setattr(panel, "adjudicate", adjudicate)


def _round(monkeypatch, capsys, tmp_path, findings, *, round_no=1, baseline=(),
           narrowed=(), name="r", config=None, max_rounds=3):
    """One whole panel run: the report it printed and the payload it wrote."""
    _stub(monkeypatch, findings, config=config)
    out = tmp_path / f"{name}{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=max_rounds, scope="pr",
                     narrowed=list(narrowed)) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def _p2(): return panel.Finding("claude", "P2", "a.py", 3, "unvalidated input", "")


def test_a_narrowed_key_ends_a_cycle_that_would_otherwise_go_again(
        monkeypatch, capsys, tmp_path):
    """End to end, because the declaration is only worth anything if the key a caller
    reads off a report reaches `round_stop` as the key that finding actually has."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    _r2, again, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                           baseline=[r1])
    assert again["round_stop"]["stop"] is False

    _r3, ended, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                           baseline=[r1], narrowed=[key], name="n")
    assert ended["round_stop"]["stop"] is True, ended["round_stop"]["reason"]
    assert ended["round_stop"]["narrowed"] == [key]
    assert ended["round_stop"]["converged"] is True


def test_the_key_is_normalised_the_way_a_finding_spells_its_own(
        monkeypatch, capsys, tmp_path):
    """A value transcribed out of a fixer's prose arrives upper-cased or padded often
    enough, and the register has to hold the spelling the finding's key equals — the
    same normalisation `--escalated` does two lines away in the parser."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    _r2, got, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                         baseline=[r1], narrowed=[f"  {key.upper()}  "])
    assert got["round_stop"]["narrowed"] == [key]
    assert got["round_stop"]["stop"] is True


def test_the_same_key_named_twice_is_recorded_once(monkeypatch, capsys, tmp_path):
    """An orchestrator building the command line out of a fixer's report can name a
    key twice — two paragraphs about one finding — and a register holding it twice
    would be a payload saying two findings were answered."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    _r2, got, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                         baseline=[r1], narrowed=[key, key.upper(), key])
    assert got["round_stop"]["narrowed"] == [key]


@pytest.mark.parametrize("junk", ["", "not-a-key", "deadbeef!", "abc"])
def test_a_value_that_is_not_a_finding_key_is_refused_out_loud(
        monkeypatch, capsys, tmp_path, junk):
    """It fails SAFE — the finding stays outstanding and the cycle goes again — which
    is exactly why it has to be said. A caller that believes it declared a narrowing
    watches the round it was trying to end run anyway, and nothing in the report
    accounts for it."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    _r2, got, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                         baseline=[r1], narrowed=[junk])
    assert any("--narrowed" in n and "is not the shape of a finding key" in n
               for n in got["config_notes"]), got["config_notes"]
    assert got["round_stop"]["stop"] is False


def test_a_key_naming_no_finding_this_round_raised_is_said_out_loud(
        monkeypatch, capsys, tmp_path):
    """The mirror of the refusal above, and the case a typo check is really for: the
    value is a well-formed key and matches nothing. A key is not a finding — a fresh
    panel that words the defect differently mints a key this cannot match — so the
    silence would leave the caller believing the declaration landed while the cycle
    ran on."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    _r2, got, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                         baseline=[r1], narrowed=[STRANGER])
    assert any(f"--narrowed {STRANGER} names no finding this round raised" in n
               for n in got["config_notes"]), got["config_notes"]
    assert got["round_stop"]["stop"] is False


def test_a_narrowing_is_NOT_inherited_by_the_next_round(monkeypatch, capsys, tmp_path):
    """The other half of the distinction from an escalation, which travels in the
    payload precisely so a cycle cannot forget it. A narrowing is DISCHARGED the
    moment it is honoured — the finding it names was fixed — so there is nothing for a
    later round to carry, and a register would be a record of decisions already spent.
    The round after therefore counts the finding again, which is the truthful answer:
    if it is still being raised, it was not fixed."""
    # A cap of 5 throughout, so round 3 below stops on the RULE rather than on the
    # counter — a capped round stops whatever the register says and would prove
    # nothing about inheritance.
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()], max_rounds=5)
    key = first["to_fix"][0]["key"]
    _r2, ended, r2 = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                            baseline=[r1], narrowed=[key], max_rounds=5)
    assert ended["round_stop"]["stop"] is True
    # Nothing in the round-2 payload carries the declaration forward...
    assert not ended.get("narrowed")
    # ...so round 3, told nothing on its own command line, goes again for it.
    _r3, third, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=3,
                           baseline=[r1, r2], name="third", max_rounds=5)
    assert third["round_stop"]["narrowed"] == []
    assert third["round_stop"]["stop"] is False, third["round_stop"]["reason"]


# ------------------------------------------------- the doors the flag is refused at

def _argv(*extra):
    return ["panel.py", "--repo", "board", "--pr", "34", *extra]


def _cli(monkeypatch, *extra):
    monkeypatch.setattr(sys, "argv", _argv(*extra))
    with pytest.raises(SystemExit) as refused:
        panel.main()
    return str(refused.value)


def test_narrowed_needs_a_cycle_to_mean_anything(monkeypatch):
    """It names a finding a FIX PASS answered, and a fix pass by construction followed
    a review round. Outside a cycle there is no round for it to clear anything in, and
    a flag accepted and ignored is a caller believing it asked for something this run
    does not do.

    `--round 1` on its own is still a single-pass review, which is why the condition
    is `round_no > 1` and not "was --round passed" — asking a different question here
    is how `--escalated` once got past both doors."""
    for outside in ((), ("--round", "1")):
        said = _cli(monkeypatch, "--narrowed", "a0b1c2d3e4f56789", *outside)
        assert "--narrowed needs a cycle to mean anything" in said, outside


def test_a_cycle_is_any_of_the_three_things_that_declare_one(monkeypatch):
    """The same three `run()`'s own `in_cycle` reads, spelled the same way: two
    conditions for one predicate is how the flag came to be accepted outside a cycle
    in the first place. Reaching `run()` is the assertion — these exit through the
    stubbed round below rather than through the door."""
    monkeypatch.setattr(panel, "run", lambda *a, **k: 0)
    for inside in (("--round", "2"), ("--max-rounds", "3"),
                   ("--baseline", "r1.json")):
        monkeypatch.setattr(sys, "argv",
                            _argv("--narrowed", "a0b1c2d3e4f56789", *inside))
        assert panel.main() == 0, inside


def test_an_ask_does_not_take_it(monkeypatch):
    """An ask puts a premise to the SEATS and is not part of a cycle at all, so a
    declaration about a fix pass is answering a question about a loop this run is not
    in. Refused rather than ignored, on the same list `--escalated` is on."""
    said = _cli(monkeypatch, "--ask", "is the mirror closed?",
                "--narrowed", "a0b1c2d3e4f56789")
    assert "--ask does not take" in said and "--narrowed" in said


def test_a_premise_declaration_does_not_take_it_either(monkeypatch):
    """Declaring a premise is a check made BEFORE a fix pass; a narrowing is a
    statement about one that has already run. Two ends of the same cycle, and neither
    is a round — so the flag is refused there for `--escalated`'s reason with the sign
    reversed."""
    said = _cli(monkeypatch, "--premise", "the mirror is closed",
                "--premise-file", "p.json", "--narrowed", "a0b1c2d3e4f56789")
    assert "--premise does not take" in said and "--narrowed" in said
