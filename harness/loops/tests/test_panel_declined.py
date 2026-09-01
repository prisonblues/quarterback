"""#665: a correction the fix pass could not make, recorded instead of discarded.

A fix pass that identifies a correction and cannot make it currently says so into
the void. On a real cycle the fixer declared two corrections it could not pay for
under the growth ceiling; the declaration went nowhere, and the next round spent
its own budget rediscovering one of them — `classify()`'s now-wrong KEEP reason —
and booked it as a fresh finding. The information existed, the fixer was honest
about it, and the loop threw it away.

`declined` is the third register of the shape `escalated` (#221) and
`acknowledged` (#547) already have: a short, keyed, round-authored record that
travels on the baseline payload and that the next round inherits instead of
rediscovering. Unlike either of them it carries a REASON, because "priced out
under a ceiling" and "I think this finding is wrong" ask the round after for
opposite things.

The invariant every test here is written around is that it **loosens nothing**.
A declined finding is still outstanding, still counted at every stop rule, still a
fix pass's work, and still buys another round exactly as it did. The register can
only ever ADD a veto line and take a cycle's claim of clean convergence away —
which is what makes it safe to be written by the one actor it reports on.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
import panel_rounds  # noqa: E402
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/tmp/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}

#: A key of the right shape that names nothing — 16 hex characters, which is what
#: `_is_key` accepts and what a finding's own key looks like.
STRANGER = "a0b1c2d3e4f56789"


def judged(severity, title="unvalidated input", file="a.py"):
    """A judge-confirmed finding, whose `.key` the test then reads."""
    reports = [panel.Finding("claude", severity, file, 3, title, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=3,
                           synthesis=title, verdict="confirmed", reported_by=reports,
                           rationale="real")


# ------------------------------------------------------- reading a declaration

@pytest.mark.parametrize("word", panel_rounds.DECLINE_REASONS)
def test_every_reason_in_the_vocabulary_is_read_as_itself(word):
    """The four words are the four ways a pass ends up not writing a patch it knows
    is owed, and each asks the next round for something different."""
    assert panel.declination_or_none(f"{STRANGER}:{word}") == (STRANGER, word, "")


def test_a_value_that_does_not_start_with_a_key_is_refused():
    """The one half the register cannot do without. A declaration nothing joins to
    is a row that matches no finding for the rest of the cycle while its caller
    reads the silence as the declaration having landed."""
    for junk in ("", "budget", "not-a-key:budget", "abc:budget"):
        assert panel.declination_or_none(junk) is None, junk


def test_an_unknown_reason_keeps_the_declaration_and_loses_only_the_word():
    """The asymmetry is the whole design. Dropping the entry would lose a declared
    defect over its adjective — this issue's own bug, committed by its fix — while
    passing the word through would put a string no vocabulary contains into the next
    round's brief and onto the board, wearing a fixer's authority."""
    key, word, problem = panel.declination_or_none(f"{STRANGER}:i ran out of time")
    assert (key, word) == (STRANGER, panel_rounds.DECLINE_UNSTATED)
    assert "is not one of" in problem and "budget" in problem


def test_a_bare_key_is_a_declaration_with_no_reason_and_says_so():
    """A fixer that names the finding and forgets the word has still told the loop
    the thing it did not know. It is recorded, and the note asks for the word."""
    key, word, problem = panel.declination_or_none(STRANGER)
    assert (key, word) == (STRANGER, panel_rounds.DECLINE_UNSTATED)
    assert "carries no reason" in problem


def test_the_key_half_is_normalised_the_way_a_finding_spells_its_own():
    """A value transcribed out of a fixer's prose arrives upper-cased or padded
    often enough, and the register has to hold the spelling the finding's key
    equals."""
    assert panel.declination_or_none(f"  {STRANGER.upper()}:BUDGET  ") == (
        STRANGER, "budget", "")


def test_a_second_colon_is_part_of_the_reason_and_not_a_third_field():
    """A key is hex and cannot contain a colon, so everything after the first is the
    word — and a caller that pasted more gets it refused as a word rather than
    silently read as the prefix."""
    key, word, problem = panel.declination_or_none(f"{STRANGER}:budget:and scope")
    assert (key, word) == (STRANGER, panel_rounds.DECLINE_UNSTATED)
    assert problem


# ------------------------------------------------------- inheriting a register

def _inherited(raw, was=2, path="b.json"):
    b = panel_rounds.Baseline()
    panel_rounds._inherit_declined(b.declined, raw, was, path, b.problems)
    return b


def test_a_declaration_carries_its_round_and_its_reason():
    got = _inherited({STRANGER: {"round": 1, "reason": "scope"}})
    assert got.declined == {STRANGER: panel_rounds.Declination(1, "scope")}
    assert got.problems == []


def test_a_bare_round_is_a_hand_written_baseline_and_is_not_a_complaint():
    """`{"<key>": 2}` means "round 2 declared this" and claims nothing about a
    reason, so nothing went wrong and nothing is reported."""
    got = _inherited({STRANGER: 1})
    assert got.declined == {STRANGER: panel_rounds.Declination(
        1, panel_rounds.DECLINE_UNSTATED)}
    assert got.problems == []


def test_a_reason_word_nobody_recognises_is_inherited_and_named():
    got = _inherited({STRANGER: {"round": 1, "reason": "vibes"}})
    assert got.declined[STRANGER].reason == panel_rounds.DECLINE_UNSTATED
    assert any("is not one of" in p for p in got.problems), got.problems


def test_the_key_and_round_failures_are_the_ones_the_shared_reader_already_has():
    """The half that matters is the failure handling, and it is `_inherit`'s — a
    third copy of it is a third chance for one copy to silently stop matching the
    others. These are its answers arriving through this register."""
    bad_shape = _inherited("not a container")
    assert any("neither an object nor a list" in p for p in bad_shape.problems)
    bad_key = _inherited({"nonsense": {"round": 1, "reason": "budget"}})
    assert bad_key.declined == {}
    assert any("not the shape of a finding key" in p for p in bad_key.problems)
    # A round later than the payload carrying it falls back to the payload's own,
    # which is never later than the truth.
    late = _inherited({STRANGER: {"round": 9, "reason": "budget"}}, was=2)
    assert late.declined[STRANGER].round == 2
    assert any("not a round of this cycle" in p for p in late.problems)


def test_the_earliest_round_owns_both_the_date_and_the_word():
    """Merging them separately would invent a declaration nobody made: round 1 said
    `budget`, round 2 re-declared the same key as `scope`, and a register holding
    round 1's date beside round 2's word is a sentence neither pass wrote."""
    b = panel_rounds.Baseline()
    panel_rounds._inherit_declined(b.declined, {STRANGER: {"round": 2,
                                                           "reason": "scope"}},
                                   2, "r2.json", b.problems)
    panel_rounds._inherit_declined(b.declined, {STRANGER: {"round": 1,
                                                           "reason": "budget"}},
                                   1, "r1.json", b.problems)
    assert b.declined == {STRANGER: panel_rounds.Declination(1, "budget")}


# ------------------------------------------------ what it does to the verdict

def test_it_subtracts_from_no_rule_and_buys_the_fixer_nothing():
    """The invariant this whole feature is bound by. A declined P1 is a P1 the fix
    pass did not fix, and rule 2 goes again for it exactly as it would if nobody had
    said anything — where an ESCALATION on the same key stops the cycle."""
    c = judged("P1")
    plain = panel.round_stop(2, 5, [], [c], [], repeated=[c.key])
    declared = panel.round_stop(2, 5, [], [c], [], repeated=[c.key],
                                declined=[c.key])
    assert plain["stop"] is declared["stop"] is False
    assert plain["reason"] == declared["reason"]
    assert declared["outstanding"]["fixable"] == plain["outstanding"]["fixable"]


def test_a_go_again_round_takes_no_veto_for_it():
    """Same "only on a STOP" rule the repeat and the escalation lines follow: on a
    round that is going again the declaration is not why the round was not quiet."""
    c = judged("P1")
    got = panel.round_stop(2, 5, [], [c], [], repeated=[c.key], declined=[c.key])
    assert got["stop"] is False
    assert got["veto"] == []


def test_a_stop_with_a_declaration_outstanding_is_not_converged():
    """The consequence the issue asks for. Nothing here made the round stop — it
    stopped on its own rules — and what the register takes away is the claim that
    stopping meant converging."""
    got = panel.round_stop(2, 5, [], [], [], declined=[STRANGER])
    assert got["stop"] is True
    assert got["confident"] is False
    assert got["converged"] is False
    assert any("declared it could not make" in v for v in got["veto"]), got["veto"]


def test_it_never_reads_as_a_dry_round():
    """The live case: the pass declined two corrections, the round after re-read
    only the fix commit, found nothing, and would have reported `dry` over a defect
    one of its own actors had already written down."""
    dry = panel.round_stop(2, 5, [], [], [])
    assert dry["reason"].startswith("dry")
    assert dry["converged"] is True
    got = panel.round_stop(2, 5, [], [], [], declined=[STRANGER])
    assert not got["reason"].startswith("dry")
    assert "ran out of corrections anybody was willing to make" in got["reason"]


def test_the_disposal_says_a_known_unfixed_defect_lands_with_the_PR():
    """`handed_to` stays `nobody` — a correction a pass already declined is by
    definition not a fixer's — but "nothing is outstanding" is not a true sentence
    to end a cycle on while one is on the record."""
    got = panel.round_stop(2, 5, [], [], [], declined=[STRANGER])
    assert got["outstanding"]["handed_to"] == "nobody"
    assert got["outstanding"]["declined"] == [STRANGER]
    assert "land with the PR" in got["outstanding"]["why"]


def test_a_live_finding_still_owns_the_reason_over_a_record_about_one():
    """Last in the chain on the chain's own rule: every branch above names work THIS
    round observed, and a live observation says more than an earlier round's note."""
    c = judged("P1")
    got = panel.round_stop(3, 3, [], [c], [], repeated=[c.key], declined=[STRANGER])
    assert "round cap" in got["reason"]
    assert got["converged"] is False


def test_the_register_is_published_under_its_own_name():
    got = panel.round_stop(2, 5, [], [], [], declined=[STRANGER, "", STRANGER])
    assert got["declined_outstanding"] == [STRANGER]


@pytest.mark.parametrize("bad", [3, "deadbeefdeadbeef"])
def test_a_count_or_a_bare_string_is_refused_rather_than_iterated(bad):
    """A bare `str` iterates character by character and matches no finding; an
    `int` is the old count contract. Both fail silently, so both are named at the
    door the way every other key collection here is."""
    with pytest.raises(TypeError):
        panel.round_stop(2, 5, [], [], [], declined=bad)


def test_an_empty_declaration_changes_nothing():
    plain = panel.round_stop(2, 5, [], [], [])
    empty = panel.round_stop(2, 5, [], [], [], declined=["", None])
    assert plain["converged"] is empty["converged"] is True
    assert empty["declined_outstanding"] == []


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
           declined=(), name="r", config=None, max_rounds=5):
    """One whole panel run: the report it printed and the payload it wrote."""
    _stub(monkeypatch, findings, config=config)
    out = tmp_path / f"{name}{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=max_rounds, scope="pr",
                     declined=list(declined)) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def _p2(): return panel.Finding("claude", "P2", "a.py", 3, "unvalidated input", "")


def test_a_declaration_travels_to_the_next_round_on_the_baseline(
        monkeypatch, capsys, tmp_path):
    """The whole point. Round 2's fixer declared the correction, round 3 was told
    nothing on its own command line, and it inherits both the key and the word."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    assert first["declined"] == {}
    _r2, second, r2 = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                             baseline=[r1], declined=[f"{key}:budget"])
    assert second["declined"] == {key: {"round": 2, "reason": "budget"}}
    _r3, third, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=3,
                           baseline=[r1, r2], name="third")
    assert third["declined"] == {key: {"round": 2, "reason": "budget"}}
    assert third["round_stop"]["declined_outstanding"] == [key]


def test_an_inherited_declaration_is_a_known_unfixed_defect_and_not_news(
        monkeypatch, capsys, tmp_path):
    """The first of the issue's three consequences. Reporting a defect an earlier
    pass already wrote down as a fresh finding overstates what the round
    discovered — and the finding carries the flag, so an orchestrator building the
    next brief can see it without joining against the register."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    assert first["to_fix"][0]["new_this_round"] is True
    assert first["to_fix"][0]["declined"] is False
    _r2, second, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                            baseline=[r1], declined=[f"{key}:refuted"])
    row = second["to_fix"][0]
    assert row["key"] == key
    assert row["declined"] is True
    assert row["new_this_round"] is False


def test_the_register_answers_even_when_the_finding_record_did_not_travel(
        monkeypatch, capsys, tmp_path):
    """Where it changes an answer at all. The register is inherited transitively and
    the finding RECORD is not, so a round given only the latest baseline — which the
    docs allow — would call an old defect new. The old answer was untrue."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    _r2, second, r2 = _round(monkeypatch, capsys, tmp_path, [], round_no=2,
                             baseline=[r1], declined=[f"{key}:budget"])
    assert second["to_fix"] == []
    # Round 3 is handed round 2's payload alone: it carries the register and no
    # record of the finding at all.
    assert second["declined"] and not second["to_fix"]
    _r3, third, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=3,
                           baseline=[r2], name="third")
    assert third["to_fix"][0]["new_this_round"] is False
    assert third["to_fix"][0]["declined"] is True


def test_it_does_not_buy_the_cycle_an_easier_stop(monkeypatch, capsys, tmp_path):
    """A key that stops being NEW becomes a REPEAT in the same breath — `panel.py`
    derives `repeated` from the same predicate — and rules 1 and 3 are both bounded
    by the trigger floor, so the four rules reach the identical verdict. The round
    goes again either way, and the declaration only takes the confidence."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    _r2, plain, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                           baseline=[r1])
    _r2b, declared, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                               baseline=[r1], declined=[f"{key}:budget"], name="d")
    assert plain["round_stop"]["stop"] is declared["round_stop"]["stop"] is False
    assert plain["round_stop"]["reason"] == declared["round_stop"]["reason"]


def test_a_cycle_that_ends_holding_one_does_not_report_a_clean_finish(
        monkeypatch, capsys, tmp_path):
    """The second consequence, end to end: the fix pass declined the correction, the
    round after it found nothing, and the verdict says what that is."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    _r2, second, r2 = _round(monkeypatch, capsys, tmp_path, [], round_no=2,
                             baseline=[r1], declined=[f"{key}:budget"])
    stop = second["round_stop"]
    assert stop["stop"] is True
    assert stop["converged"] is False
    assert any("declared it could not make" in v for v in stop["veto"]), stop["veto"]


def test_the_report_marks_the_row_with_the_round_and_the_word(
        monkeypatch, capsys, tmp_path):
    """A reader deciding whether to raise a ceiling, widen a scope or argue with the
    fixer needs the word, and cannot get it anywhere else in the report."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    report, _second, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                                baseline=[r1], declined=[f"{key}:scope"])
    assert "declared unfixable in round 2 (scope)" in report
    assert "known-unfixed defect, not a fresh finding" in report


def test_a_key_naming_nothing_this_cycle_saw_is_said_out_loud(
        monkeypatch, capsys, tmp_path):
    """A mistyped `--declined` key leaves the DECLARATION nowhere: the register
    holds a row that joins to nothing, the next round inherits it, and the
    correction the pass was honest about is rediscovered anyway."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    _r2, got, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                         baseline=[r1], declined=[f"{STRANGER}:budget"])
    assert any(f"--declined {STRANGER} names no finding this round raised" in n
               for n in got["config_notes"]), got["config_notes"]


@pytest.mark.parametrize("junk", ["", "not-a-key", "deadbeef!:budget", "abc:scope"])
def test_a_value_with_no_key_in_it_is_refused_out_loud(
        monkeypatch, capsys, tmp_path, junk):
    """It fails safe — nothing is recorded — which is exactly why it has to be said:
    the caller believes the correction was written down and the next round pays to
    rediscover it anyway."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    _r2, got, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                         baseline=[r1], declined=[junk])
    assert got["declined"] == {}
    assert any("--declined" in n and "does not start with a finding key" in n
               for n in got["config_notes"]), got["config_notes"]


def test_the_same_declaration_named_twice_is_recorded_once(
        monkeypatch, capsys, tmp_path):
    """An orchestrator building the command line out of a fixer's report can name a
    key twice, and `panel-review-pr.md` documents re-passing an inherited one as
    harmless. It has to actually be."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    _r2, got, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                         baseline=[r1],
                         declined=[f"{key}:budget", f"{key.upper()}:budget"])
    assert got["declined"] == {key: {"round": 2, "reason": "budget"}}


def test_a_round_that_reviewed_nothing_says_the_declaration_was_lost(
        monkeypatch, capsys, tmp_path):
    """`--escalated`'s answer for its reason: a skipped round cannot date a
    declaration to itself, and the typo check that would catch a mistyped key needs
    findings it does not have. Silence is the one option ruled out."""
    skipping = {**CFG, "review_panel": {"skip_title_patterns": ["^fix: "]}}
    _r1, got, _ = _round(monkeypatch, capsys, tmp_path, [], round_no=2,
                         baseline=[], declined=[f"{STRANGER}:budget"],
                         config=skipping)
    assert got["reviewed"] is False
    assert any(f"--declined {STRANGER} was passed to a round that reviewed nothing"
               in n for n in got["config_notes"]), got["config_notes"]


def test_a_skipped_round_carries_an_inherited_register_forward(
        monkeypatch, capsys, tmp_path):
    """A correction an earlier pass could not make is not made by a title matching
    /^Merge /, and a register that emptied on the quietest round of the cycle would
    lose the fact on the round least likely to be read."""
    _r1, first, r1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = first["to_fix"][0]["key"]
    _r2, second, r2 = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                             baseline=[r1], declined=[f"{key}:premise"])
    skipping = {**CFG, "review_panel": {"skip_title_patterns": ["^fix: "]}}
    _r3, third, _ = _round(monkeypatch, capsys, tmp_path, [], round_no=3,
                           baseline=[r1, r2], name="third", config=skipping)
    assert third["reviewed"] is False
    assert third["declined"] == {key: {"round": 2, "reason": "premise"}}


# ---------------------------------------------------------- reaching the board

def test_a_cycle_that_ENDS_holding_declarations_announces_them(monkeypatch):
    """The third consequence. A declaration that lives only in the payload is read
    by the next round and by nobody else, so a PR that lands with known-unfixed
    defects lands with them unnamed exactly as it does today."""
    posted = {}

    def fake(**kw):
        posted.update(kw)
        return "announced"

    monkeypatch.setattr(panel, "announce", fake)
    payload = {"github": "acme/board", "pr": 34, "head_sha": "a" * 40,
               "branch": "h",
               "declined": {STRANGER: {"round": 2, "reason": "budget"}},
               "round_stop": {"stop": True, "reason": "dry",
                              "declined_outstanding": [STRANGER]}}
    assert panel.announce_declinations(payload, CFG) == ["announced"]
    assert posted["cls"] == "decision"
    assert STRANGER[:12] in posted["detail"]
    assert "budget" in posted["detail"]
    assert "known-unfixed" in posted["summary"]


def test_a_round_that_is_going_again_announces_nothing(monkeypatch):
    """A declaration on a `go again` round is a fact about work still in flight —
    the next fix pass may well make the correction — and announcing it would put a
    question on a person's queue that the loop is still answering."""
    monkeypatch.setattr(panel, "announce", lambda **kw: "announced")
    payload = {"github": "acme/board", "pr": 34, "head_sha": "a" * 40,
               "declined": {STRANGER: {"round": 2, "reason": "budget"}},
               "round_stop": {"stop": False, "declined_outstanding": [STRANGER]}}
    assert panel.announce_declinations(payload, CFG) == []


def test_a_clean_cycle_announces_nothing_either(monkeypatch):
    monkeypatch.setattr(panel, "announce", lambda **kw: "announced")
    payload = {"github": "acme/board", "pr": 34,
               "round_stop": {"stop": True, "declined_outstanding": []}}
    assert panel.announce_declinations(payload, CFG) == []


# ------------------------------------------------- the doors the flag is refused at

def _argv(*extra):
    return ["panel.py", "--repo", "board", "--pr", "34", *extra]


def _cli(monkeypatch, *extra):
    monkeypatch.setattr(sys, "argv", _argv(*extra))
    with pytest.raises(SystemExit) as refused:
        panel.main()
    return str(refused.value)


def test_declined_needs_a_cycle_to_mean_anything(monkeypatch):
    """Outside a cycle there is no later round to inherit the declaration, which is
    the only thing the register is for — recording one there would write a fact into
    a payload nothing will ever read."""
    for outside in ((), ("--round", "1")):
        said = _cli(monkeypatch, "--declined", f"{STRANGER}:budget", *outside)
        assert "--declined needs a cycle to mean anything" in said, outside


def test_a_cycle_is_any_of_the_three_things_that_declare_one(monkeypatch):
    """The same three `run()`'s own `in_cycle` reads, spelled the same way."""
    monkeypatch.setattr(panel, "run", lambda *a, **k: 0)
    for inside in (("--round", "2"), ("--max-rounds", "3"),
                   ("--baseline", "r1.json")):
        monkeypatch.setattr(sys, "argv",
                            _argv("--declined", f"{STRANGER}:budget", *inside))
        assert panel.main() == 0, inside


def test_an_ask_does_not_take_it(monkeypatch):
    said = _cli(monkeypatch, "--ask", "is the mirror closed?",
                "--declined", f"{STRANGER}:budget")
    assert "--ask does not take" in said and "--declined" in said


def test_a_premise_declaration_does_not_take_it_either(monkeypatch):
    """Declaring a premise is a check made BEFORE a fix pass; declining a correction
    is a statement about one that has already run."""
    said = _cli(monkeypatch, "--premise", "the mirror is closed",
                "--premise-file", "p.json", "--declined", f"{STRANGER}:budget")
    assert "--premise does not take" in said and "--declined" in said
