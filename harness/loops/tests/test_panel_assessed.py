"""Retiring a coverage declaration somebody went and answered (#718).

A `could_not_assess` line vetoes a round's confident stop, and until this register
existed there was no way to retire one. `--acknowledge` takes `uc-` claim keys only,
so a coverage declaration was permanent for the life of that round's record whatever
anybody subsequently learned — and `preland`'s `review` check reads `stop_confident`
off the board row and lists the vetoes verbatim, so an answered question went on
holding the landing exactly as hard as an unanswered one.

That is #716's shape one layer out. There, evidence a seat *could not gather* had no
channel into the round; here, evidence somebody *did* gather has no channel back into
it. Measured on lexray#1631 round 2: two of its three vetoes were `could_not_assess`
by the claude seat, which cannot execute anything, and both were closed from a
worktree pinned at the PR head inside ten minutes. `preland` reported both verbatim
afterwards.

The three properties these defend:

1. **The key is the declaration.** Content-addressed the way `uc-` keys are, printed
   beside the declaration in the report, and in a third key space so it can be passed
   to neither of the two doors already there.
2. **Per declaration, never in bulk.** A blanket yes is the cheap gate, and a gate
   that always passes is worse than one that always holds because it looks like
   assurance. There is no flag that answers them all.
3. **The answer is recorded, not trusted.** Round, note and claimed assessor, marked
   unattested rather than refused (#40) and rendered as a claim rather than a
   signature — which is strictly more than the status quo, where the answer is
   recorded nowhere at all.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_rounds  # noqa: E402
import preland  # noqa: E402
from conftest import gh_stub  # noqa: E402

#: The gap the seats in this file declare, and the key `declaration_key` mints for
#: it. Written out rather than derived from the function under test: a key the tests
#: compute the same way the code does agrees with any derivation, including a broken
#: one, and this is the string a person types back off a PR comment.
GAP = "whether the fresh replay asserts anything about the ten seeded rows"
GAP_KEY = "ca-6364b7ed36f8"

#: A second one, so "per declaration" can be told from "per round".
OTHER_GAP = "whether the ambient test.env leaves any legacy var set"
OTHER_KEY = "ca-d89014fa3ff8"

#: What actually closed the first one, on the PR that measured it.
NOTE = "ran the DB suite at this head: exactly 10 rows, ics_invite absent, idempotent"


def _meta(**over):
    """One seat that ran, read the whole diff and declared :data:`GAP`."""
    row = {"ran": True, "could_not_assess": [GAP], "truncated": False}
    return {"claude": {**row, **over}}


def _veto(meta=None, **kw):
    """`coverage_veto` on a round that is clean but for its declarations."""
    kw.setdefault("ci_status", "PASS")
    return panel.coverage_veto(meta if meta is not None else _meta(), None, 0, 100, **kw)


# ---- the key ---------------------------------------------------------------


def test_the_key_is_derived_from_the_declaration_and_nothing_else():
    """Content-addressed, so a seat that declares the same gap next round declares it
    under the same key and one answer discharges it for the rest of the cycle. That
    is the whole reason the register survives a round boundary at all."""
    assert panel.declaration_key(GAP) == GAP_KEY
    assert panel.declaration_key(OTHER_GAP) == OTHER_KEY


@pytest.mark.parametrize("spelling", (
    GAP.upper(),
    f"  {GAP}  ",
    GAP.replace(" ", "\n  "),
    GAP + ".",
))
def test_one_declaration_keeps_one_key_across_the_spellings_a_rewrite_changes(spelling):
    """`_claim_norm`'s limit, stated rather than papered over: case, run-together
    whitespace and a trailing stop are absorbed, and rewording is not. A seat that
    says the same thing in different words mints a different key — which is REPORTED
    rather than matched, because this design refuses to compare declaration prose."""
    assert panel.declaration_key(spelling) == GAP_KEY


def test_a_reworded_declaration_mints_a_new_key_rather_than_being_matched():
    assert panel.declaration_key("whether that replay asserts anything at all") != GAP_KEY


@pytest.mark.parametrize("raw", (
    "", "deadbeefdeadbeef", "ca-", "ca-nothexvalue1", "ca-1234abcd",
    "ca-1234abcdef012", "uc-f1554b5ef264",
))
def test_a_value_that_is_not_a_declaration_key_is_not_one(raw):
    assert not panel.is_declaration_key(raw)


def test_a_declaration_key_is_not_an_obligation_key_and_the_two_cannot_be_swapped():
    """Three vocabularies now meet in the argument parser — a finding key (bare hex),
    an obligation key (`uc-`) and a declaration key (`ca-`) — and each door refuses
    the other two's keys. A key accepted at the wrong door matches nothing for the
    rest of the cycle while its caller reads the silence as the veto lifted."""
    ca, uc = panel.declaration_key(GAP), panel.claim_key(GAP)
    assert ca != uc
    assert panel.is_declaration_key(ca) and not panel.is_claim_key(ca)
    assert panel.is_claim_key(uc) and not panel.is_declaration_key(uc)
    assert not panel_rounds._is_key(ca) and not panel_rounds._is_key(uc)


# ---- what an assessment lifts, and what it does not -------------------------


def test_an_unassessed_declaration_still_costs_the_round_its_confidence():
    """The behaviour before this register, unchanged: nothing here loosens the gate
    by default, and a round that passes no flag is the round it always was."""
    assert _veto() == [f"claude could not assess: {GAP}"]
    assert panel.round_stop(1, 2, [], [], _veto())["confident"] is False


def test_answering_the_declaration_by_key_is_what_lifts_the_veto():
    assert _veto(assessed=[GAP_KEY]) == []
    assert panel.round_stop(1, 2, [], [], _veto(assessed=[GAP_KEY]))["confident"] is True


def test_answering_one_declaration_leaves_every_other_one_vetoing():
    """Per declaration, and this is the assertion that says so at the level the rule
    actually lives at. A flag that lifted the round's whole coverage list on one key
    would be the blanket yes by the back door."""
    meta = _meta(could_not_assess=[GAP, OTHER_GAP])
    assert _veto(meta, assessed=[GAP_KEY]) == [f"claude could not assess: {OTHER_GAP}"]


def test_answering_a_declaration_lifts_it_for_every_seat_that_raised_it():
    """Merged by the declaration, the way an obligation is merged by the claim: four
    seats stating one gap are one question, and answering it four times is four
    chances to answer it differently."""
    meta = {"claude": {"ran": True, "could_not_assess": [GAP]},
            "codex": {"ran": True, "could_not_assess": [GAP]}}
    assert len(_veto(meta)) == 2
    assert _veto(meta, assessed=[GAP_KEY]) == []


def test_an_assessment_lifts_nothing_but_the_declaration_it_names():
    """Every other veto in the function is a different fact, and a person answering a
    coverage question has not touched one of them. A CI channel with no settled
    result is the sharpest case: it is the one that says nothing mechanical ran."""
    why = _veto(ci_status="none", assessed=[GAP_KEY])
    assert why == ["no CI run exists for this commit — nothing mechanical executed "
                   "this code"]


@pytest.mark.parametrize("spelling", (f"  {GAP_KEY.upper()}  ", GAP_KEY.upper()))
def test_a_key_a_human_retyped_is_normalised_rather_than_ignored(spelling):
    """A key travels through a shell, a PR comment and a clipboard before it gets
    here. A padded spelling that silently matched nothing would leave the caller
    reading the veto as one they had already answered."""
    assert _veto(assessed=[spelling]) == []


def test_a_declaration_the_judge_ruled_unresolvable_is_answered_at_the_other_door():
    """The two registers are disjoint by construction. A declaration ruled
    structurally unanswerable is an obligation and is discharged by `--acknowledge`;
    one that is merely unanswered is a declaration and is discharged by `--assessed`.
    A declaration answerable at either door is one a caller closes at the door that
    asks for less, and the whole point of #547's is that it asks for a decision."""
    ob = panel.Obligation(panel.claim_key(GAP), GAP, "needs a database")
    ruling = panel.CoverageRuling("", {("claude", GAP): ob})
    assert panel.reached_declarations(_meta(), ruling) == ()
    why = _veto(coverage=ruling, assessed=[panel.declaration_key(GAP)])
    assert why == [f"an unverifiable claim is unacknowledged [{ob.key}]: {GAP} — "
                   "needs a database"]


def test_a_seat_whose_copy_of_a_gap_was_ruled_is_not_offered_the_other_door(
        monkeypatch, capsys, tmp_path):
    """Two seats can raise the identical gap and the judge can rule one of them
    unresolvable and leave the other unruled — the ruling is per numbered
    `(reviewer, declaration)` entry, and nothing stops it.

    The ruled seat's line is an obligation and is answered at `--acknowledge`. Printing
    the unruled seat's `ca-` key beside it would tell the reader a door is open that is
    not, and — once the key was passed — mark that line "✅ assessed" while the
    declaration went on vetoing under `uc-`."""
    ruled = panel.Obligation(panel.claim_key(GAP), GAP, "needs a database")
    ruling = panel.CoverageRuling("", {("claude", GAP): ruled})
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: {
        **CFG, "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                             "codex": {"enabled": True, "model": "gpt"}}})
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1, "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, [GAP]))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], None, ruling))
    out = tmp_path / "r1.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=1, max_rounds=3,
                     assessed=[f"{GAP_KEY}:{NOTE}"]) == 0
    report = capsys.readouterr().out
    got = json.loads(out.read_text())
    # The ledger holds one declaration, and it belongs to the unruled seat alone.
    assert [(d["key"], d["seats"]) for d in got["coverage_declarations"]] == [
        (GAP_KEY, ["codex"])]
    # The claude line carries no `ca-` key; the codex line does, and is answered.
    under = {}
    seat = None
    for line in report.splitlines():
        if line.startswith("- **") and "could not assess" in line:
            seat = line.split("**")[1]
            under[seat] = []
        elif seat and line.startswith("  - "):
            under[seat].append(line)
        elif not line.startswith("  "):
            seat = None
    assert under["claude"] == [f"  - {GAP}"], "no key: this one is an obligation"
    assert len(under["codex"]) == 1 and under["codex"][0].startswith(f"  - `{GAP_KEY}`")
    assert "✅ assessed in round 1" in under["codex"][0]
    # And the obligation is still vetoing, which is what the report must not deny.
    assert got["round_stop"]["confident"] is False
    assert any(ruled.key in v for v in got["round_stop"]["veto"])


def test_a_blind_seats_declaration_never_acquires_a_key_to_answer():
    """A code-blind seat's declarations are reported and do not vote, so they cost
    the round nothing today. Giving one a key would print a remedy for a veto that
    does not exist, and a remedy for nothing is how a register teaches its reader
    that the keys mean nothing."""
    meta = _meta(code_blind=True)
    assert _veto(meta) == []
    assert panel.reached_declarations(meta) == ()


def test_a_seat_that_never_ran_raises_no_declaration_to_answer():
    meta = _meta(ran=False, skip="claude CLI absent", absent=True)
    assert panel.reached_declarations(meta) == ()


def test_the_ledger_records_every_seat_that_raised_the_declaration():
    meta = {"claude": {"ran": True, "could_not_assess": [GAP]},
            "codex": {"ran": True, "could_not_assess": [GAP]}}
    assert panel.reached_declarations(meta) == (
        panel.Declaration(GAP_KEY, GAP, ("claude", "codex")),)


# ---- reading one off the command line --------------------------------------


def test_the_flag_takes_a_key_and_what_was_measured():
    assert panel.assessment_or_none(f"{GAP_KEY}:{NOTE}") == (GAP_KEY, NOTE, "")


def test_the_note_keeps_every_colon_after_the_first():
    """A note is very often somebody's shell commands, and a key is a prefix and hex
    and cannot contain a colon — so everything after the first one is the note."""
    key, said, problem = panel.assessment_or_none(f"{GAP_KEY}:ran: psql -c 'select 1'")
    assert (key, said, problem) == (GAP_KEY, "ran: psql -c 'select 1'", "")


def test_a_bad_key_is_refused_outright():
    """The one half the register cannot do without: an assessment nothing joins to
    discharges no declaration while its caller reads the silence as success."""
    assert panel.assessment_or_none(f"uc-f1554b5ef264:{NOTE}") is None
    assert panel.assessment_or_none(NOTE) is None


def test_a_missing_note_is_named_and_recorded_rather_than_refused():
    """The asymmetry `--declined` already has, for its reason. Refusing here would
    leave the veto standing on a question that has in fact been answered, which is
    the defect this register exists to remove, re-committed by its own fix."""
    key, said, problem = panel.assessment_or_none(GAP_KEY)
    assert (key, said) == (GAP_KEY, "")
    assert "carries no note" in problem and "KEY:NOTE" in problem


# ---- one whole round -------------------------------------------------------

CFG = {"github": "acme/board", "path": "/tmp/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}

#: The same repo with a title pattern the skip path fires on — a merge commit, the
#: case the skip path exists for.
SKIPPING = {**CFG, "review_panel": {"skip_title_patterns": ["^Merge "]}}


def _round(monkeypatch, capsys, tmp_path, *, assessed=(), assessed_by="",
           baseline=(), round_no=1, gaps=(GAP,), title="feat: x", cfg=CFG):
    """One whole cycle round whose single seat declares a gap no judge rules on —
    the shape of the round #718 was filed off. Returns (report, payload)."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": title, "additions": 3, "deletions": 1, "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, list(gaps)))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, max_rounds=3, baseline=list(baseline),
                     assessed=list(assessed), assessed_by=assessed_by) == 0
    return capsys.readouterr().out, json.loads(out.read_text())


def test_every_coverage_declaration_reaches_the_payloads_ledger(monkeypatch, capsys,
                                                                tmp_path):
    """A content-addressed key nobody is shown is a key nobody types back. The ledger
    is what makes the register usable at all, and whether somebody has answered a
    declaration is a separate field so a reader can tell "this round raised none"
    from "all of them are answered"."""
    _report, got = _round(monkeypatch, capsys, tmp_path)
    assert got["coverage_declarations"] == [
        {"key": GAP_KEY, "declaration": GAP, "seats": ["claude"], "assessed": False,
         "note": "", "assessed_by": None, "attested": False}]
    assert got["assessed"] == {}


def test_the_report_names_the_declaration_its_key_and_the_command_that_answers_it(
        monkeypatch, capsys, tmp_path):
    """The remedy in the artefact, because a veto whose remedy lives in a brief the
    reader does not have open is a veto they resolve by dropping the gate."""
    report, _got = _round(monkeypatch, capsys, tmp_path)
    assert f"`{GAP_KEY}` — {GAP}" in report
    assert "⏳ **unassessed**" in report
    assert f"--assessed {GAP_KEY}:" in report
    assert "there is no flag that answers them all" in report


def test_an_answered_declaration_stays_on_the_ledger_and_stops_costing_the_round(
        monkeypatch, capsys, tmp_path):
    """It is not deleted. The declaration was true when the seat wrote it, and a
    register that erased what it discharged would leave the round's record claiming
    the question was never raised."""
    report, got = _round(monkeypatch, capsys, tmp_path,
                         assessed=[f"{GAP_KEY}:{NOTE}"], assessed_by="rich")
    assert got["coverage_declarations"] == [
        {"key": GAP_KEY, "declaration": GAP, "seats": ["claude"], "assessed": True,
         "note": NOTE, "assessed_by": "rich", "attested": True}]
    assert got["assessed"] == {
        GAP_KEY: {"round": 1, "note": NOTE, "set_by": "rich", "attested": True}}
    assert got["round_stop"]["confident"] is True
    assert got["round_stop"]["veto"] == []
    assert GAP in report


def test_the_note_is_what_the_report_shows_beside_the_answer(monkeypatch, capsys,
                                                             tmp_path):
    """Unlike a claim, the answer is a fact somebody measured rather than a risk
    somebody accepted, and recording WHAT closed it is most of the value."""
    report, _got = _round(monkeypatch, capsys, tmp_path,
                          assessed=[f"{GAP_KEY}:{NOTE}"], assessed_by="rich")
    assert f"✅ assessed in round 1, rich says so — _{NOTE}_" in report


def test_an_answer_with_no_note_says_so_where_a_reader_will_see_it(monkeypatch, capsys,
                                                                   tmp_path):
    report, got = _round(monkeypatch, capsys, tmp_path, assessed=[GAP_KEY])
    assert got["assessed"][GAP_KEY]["note"] == ""
    assert "no note: nothing records what closed it" in report
    assert any("carries no note" in n for n in got["config_notes"])


def test_answering_one_declaration_of_two_leaves_the_other_vetoing(monkeypatch, capsys,
                                                                   tmp_path):
    """The rule at the level a caller meets it: two declarations, one flag, one veto
    left. There is no invocation that answers both without naming both."""
    _report, got = _round(monkeypatch, capsys, tmp_path, gaps=(GAP, OTHER_GAP),
                          assessed=[f"{GAP_KEY}:{NOTE}"])
    assert got["round_stop"]["veto"] == [f"claude could not assess: {OTHER_GAP}"]
    assert got["round_stop"]["confident"] is False
    assert [d["assessed"] for d in got["coverage_declarations"]] == [True, False]


def test_there_is_no_flag_that_answers_every_declaration_at_once():
    """The claims register's rule, for its reason. A blanket yes is the cheap gate,
    and a gate that always passes is worse than one that always holds because it
    looks like assurance."""
    source = (Path(__file__).resolve().parent.parent / "panel.py").read_text()
    for spelling in ("--assess-all", "--assessed-all", "--assess-every",
                     "--all-assessed"):
        assert spelling not in source


# ---- who says so ------------------------------------------------------------


def test_an_unattested_answer_is_recorded_as_unattested_rather_than_refused(
        monkeypatch, capsys, tmp_path):
    """#40's rule. The round that answered the declaration is also the round it was
    answered for, which is the actor attesting to its own work — the objection
    `--escalated` already carries and `record-outcome`'s `refuted` already answers.
    Refusing would leave the answer where it is today: in a PR comment nothing
    counts."""
    report, got = _round(monkeypatch, capsys, tmp_path,
                         assessed=[f"{GAP_KEY}:{NOTE}"])
    assert got["assessed"][GAP_KEY]["set_by"] == "unattested"
    assert got["assessed"][GAP_KEY]["attested"] is False
    assert got["coverage_declarations"][0]["attested"] is False
    assert got["round_stop"]["confident"] is True, "recorded, not refused"
    assert "**unattested** — the round's own caller" in report
    assert any("marked unattested rather than refused (#40)" in n
               for n in got["config_notes"])


def test_a_named_assessor_is_rendered_as_a_claim_and_not_a_signature(monkeypatch,
                                                                     capsys, tmp_path):
    """"rich says so", never "signed off by rich". Nothing here can authenticate a
    person and an agent that wants to type a human's name can, so the name is stored
    beside the round that claimed it and published as a claim."""
    report, got = _round(monkeypatch, capsys, tmp_path,
                         assessed=[f"{GAP_KEY}:{NOTE}"], assessed_by="rich")
    assert "rich says so" in report and "signed off by" not in report
    assert got["assessed"][GAP_KEY]["attested"] is True
    assert not any("marked unattested" in n for n in got["config_notes"])


# ---- across a cycle ---------------------------------------------------------


def test_an_assessment_is_inherited_by_the_next_round(monkeypatch, capsys, tmp_path):
    """Done once per cycle and not once per round, which is `--acknowledge`'s
    property and the sharper half of the fix: a cycle that forgot the answer between
    rounds would put a question somebody already measured back in front of them."""
    _r1, first = _round(monkeypatch, capsys, tmp_path,
                        assessed=[f"{GAP_KEY}:{NOTE}"], assessed_by="rich")
    assert first["assessed"][GAP_KEY]["round"] == 1
    _r2, second = _round(monkeypatch, capsys, tmp_path, round_no=2,
                         baseline=[str(tmp_path / "r1.json")])
    assert second["assessed"] == {
        GAP_KEY: {"round": 1, "note": NOTE, "set_by": "rich", "attested": True}}
    assert second["round_stop"]["veto"] == []
    assert second["round_stop"]["confident"] is True


def test_the_earliest_round_that_answered_owns_the_date_the_note_and_the_assessor(
        monkeypatch, capsys, tmp_path):
    """A caller re-passing a key it inherited must not re-date the assessment — or
    reattribute it, which is the sharper version of the same rule here, since the
    entry carries somebody's name."""
    _r1, _first = _round(monkeypatch, capsys, tmp_path,
                         assessed=[f"{GAP_KEY}:{NOTE}"], assessed_by="rich")
    _r2, second = _round(monkeypatch, capsys, tmp_path, round_no=2,
                         baseline=[str(tmp_path / "r1.json")],
                         assessed=[f"{GAP_KEY}:something else"], assessed_by="somebody")
    assert second["assessed"][GAP_KEY] == {
        "round": 1, "note": NOTE, "set_by": "rich", "attested": True}


def test_an_assessment_naming_no_declaration_this_round_raised_is_said_out_loud(
        monkeypatch, capsys, tmp_path):
    """The likeliest explanation is a seat that reworded its own declaration, which
    the key deliberately cannot absorb. The alternatives are a typo and a gap that
    stopped being declared, and nothing here can tell the three apart — what it can
    do is stop the caller reading the silence as the answer having landed."""
    _report, got = _round(monkeypatch, capsys, tmp_path,
                          assessed=[f"{OTHER_KEY}:{NOTE}"])
    assert any(f"--assessed {OTHER_KEY} names no coverage declaration this round "
               "raised" in n for n in got["config_notes"])


def test_an_inherited_assessment_that_names_nothing_is_not_reported_every_round(
        monkeypatch, capsys, tmp_path):
    """An inherited assessment names no declaration in the ordinary case where it
    WORKED — the seat stopped declaring the gap because somebody answered it — so
    reporting that every round is the alert fatigue these notes are careful not to
    become."""
    _r1, _first = _round(monkeypatch, capsys, tmp_path,
                         assessed=[f"{GAP_KEY}:{NOTE}"])
    _r2, second = _round(monkeypatch, capsys, tmp_path, round_no=2, gaps=(),
                         baseline=[str(tmp_path / "r1.json")])
    assert second["assessed"][GAP_KEY]["round"] == 1
    assert not any("names no coverage declaration" in n
                   for n in second["config_notes"])


@pytest.mark.parametrize("raw", ("", "deadbeefdeadbeef", "ca-nothex12345",
                                 "uc-f1554b5ef264", f"uc-f1554b5ef264:{NOTE}"))
def test_a_value_that_is_not_a_declaration_key_is_refused_and_says_what_that_costs(
        monkeypatch, capsys, tmp_path, raw):
    _report, got = _round(monkeypatch, capsys, tmp_path, assessed=[raw])
    assert got["assessed"] == {}
    assert got["round_stop"]["confident"] is False
    assert any("is not the shape of a declaration key" in n
               and "still costs the round its confidence" in n
               for n in got["config_notes"])


def test_a_malformed_key_is_not_echoed_raw_into_the_report(monkeypatch, capsys,
                                                           tmp_path):
    """`config_notes` reaches a public PR comment under `--post`, so the echo goes
    through `_key_gist` exactly as `--escalated`'s and `--acknowledge`'s do."""
    _report, got = _round(monkeypatch, capsys, tmp_path,
                          assessed=["<script>alert(1)</script>"])
    assert not any("<script>" in n for n in got["config_notes"])


def test_a_round_that_reviewed_nothing_records_no_assessment_and_says_so(
        monkeypatch, capsys, tmp_path):
    """`--acknowledge`'s answer and not `--retract`'s, which is the choice worth
    pinning because the flags are so alike. A retraction is an act about a key
    already in a register; an assessment is an act about a DECLARATION, and a round
    that reviewed nothing has none — dating one to it would write the answer in
    against a question this round never asked."""
    _report, got = _round(monkeypatch, capsys, tmp_path, title="Merge branch 'main'",
                          cfg=SKIPPING, assessed=[f"{GAP_KEY}:{NOTE}"])
    assert got["skip_reason"]
    assert got["assessed"] == {}
    assert any(f"--assessed {GAP_KEY} was passed to a round that reviewed nothing"
               in n for n in got["config_notes"])


def test_a_skipped_round_carries_the_cycles_assessments_forward(monkeypatch, capsys,
                                                                tmp_path):
    """A declaration somebody measured the answer to has not become unanswered
    because a title matched /^Merge /, and a register that emptied on the quietest
    round of the cycle would put the question back on the round least likely to be
    read."""
    _r1, _first = _round(monkeypatch, capsys, tmp_path,
                         assessed=[f"{GAP_KEY}:{NOTE}"], assessed_by="rich")
    _r2, second = _round(monkeypatch, capsys, tmp_path, round_no=2,
                         title="Merge branch 'main'", cfg=SKIPPING,
                         baseline=[str(tmp_path / "r1.json")])
    assert second["assessed"] == {
        GAP_KEY: {"round": 1, "note": NOTE, "set_by": "rich", "attested": True}}


# ---- reading a baseline that is not what it should be ----------------------


def _baseline(tmp_path, register, name="r1.json"):
    (tmp_path / name).write_text(json.dumps(
        {"round": 1, "reviewed": True, "head_sha": "abc", "assessed": register}))
    return panel.load_baseline([str(tmp_path / name)])


@pytest.mark.parametrize("register,expect_problem", (
    ({GAP_KEY: {"round": 1, "note": NOTE, "set_by": "rich"}}, False),
    ({GAP_KEY: 1}, False),
    ([GAP_KEY], False),
    ({GAP_KEY: {"round": 9, "note": NOTE}}, True),
    ({"deadbeefdeadbeef": {"round": 1}}, True),
    ("not-a-register", True),
))
def test_what_a_malformed_assessed_register_costs(tmp_path, register, expect_problem):
    """Tolerant and loud, the rule its four siblings keep: an unreadable register
    reverts the cycle to asking somebody a question they have already answered, and
    it must not arrive with nothing said."""
    b = _baseline(tmp_path, register)
    assert bool(b.problems) is expect_problem
    if expect_problem and b.problems:
        assert any("assessed" in p for p in b.problems)


def test_a_bare_round_in_the_register_is_read_as_that_round_with_nothing_claimed(
        tmp_path):
    """A hand-written baseline saying `{"ca-…": 1}` means "round 1 recorded this",
    with no note and nobody named. Nothing went wrong — the payload never claimed to
    carry either — so it is read and not reported."""
    b = _baseline(tmp_path, {GAP_KEY: 1})
    assert b.assessed == {GAP_KEY: panel.Assessment(1, "", "unattested")}
    assert not b.problems


def test_a_bare_list_names_the_round_that_wrote_it_and_claims_nothing_else(tmp_path):
    b = _baseline(tmp_path, [GAP_KEY])
    assert b.assessed == {GAP_KEY: panel.Assessment(1, "", "unattested")}


def test_an_unusable_assessor_is_read_as_unattested_rather_than_dropped(tmp_path):
    """The only thing this can do with an unusable name is record it as unattested —
    which is what an absent one records too, and is the honest reading of both.
    Dropping the entry would put a veto back on an answered question over the
    spelling of a field that proves nothing either way."""
    b = _baseline(tmp_path, {GAP_KEY: {"round": 1, "note": NOTE, "set_by": 17}})
    assert b.assessed[GAP_KEY] == panel.Assessment(1, NOTE, "unattested")
    assert not b.problems


# ---- the gate the fix exists to unblock -------------------------------------


def test_preland_stops_holding_on_a_declaration_somebody_answered(monkeypatch, capsys,
                                                                  tmp_path):
    """The end of the chain, and the reason the subtraction lives in `coverage_veto`
    rather than in `preland`. `preland.check_review` reads `stop_confident` off the
    board row and lists `stop_veto` verbatim — so a declaration that stops being a
    veto stops being reported, with no second implementation of the rule and no
    change to the region #717 is separately reworking.

    Both halves asserted from ONE round's real payload: the strict reading HOLDs on
    the unanswered declaration and is READY once it is answered."""
    _report, unanswered = _round(monkeypatch, capsys, tmp_path)
    _report2, answered = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[str(tmp_path / "r1.json")],
                                assessed=[f"{GAP_KEY}:{NOTE}"], assessed_by="rich")

    def row(payload):
        stop = payload["round_stop"]
        return {"id": 1, "ts": "2026-09-03T00:00:00+00:00", "round": payload["round"],
                "cycle": "f5c76fd8", "head_sha": "a" * 40, "stopped": True,
                "stop_reason": stop["reason"], "stop_confident": stop["confident"],
                "stop_veto": stop["veto"], "confirmed": 0, "unjudged": 0,
                "sonar_gate": "OK", "judge_skip": None}

    hold = preland._judge_round(preland.Check("review", "passed"), row(unanswered),
                                {"headRefOid": "a" * 40}, earned_stop=True)
    assert hold.status == "failed"
    assert hold.reasons == [f"the stop was not earned: claude could not assess: {GAP}"]

    ready = preland._judge_round(preland.Check("review", "passed"), row(answered),
                                 {"headRefOid": "a" * 40}, earned_stop=True)
    assert ready.status == "passed" and not ready.reasons and not ready.warnings
