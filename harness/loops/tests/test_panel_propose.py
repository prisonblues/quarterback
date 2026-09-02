"""#507's constructive pass: on an escalation, ask the seats what they would DO.

The three convergence issues around this one all END the cycle — #489 stops it,
#505 stops it earlier, #506 offers to undo the change that caused it. All three
are subtractive. This is the only one that puts information IN, and it is the one
that gives a human at the veto line something to act on other than a list of
complaints.

What is pinned here is the four things that make it a mechanism rather than a
paragraph, in the shape `test_panel_volume.py` pins its own:

* the TRIGGER — an `escalate_on` rung that FIRED, never a measurement that merely
  crossed, never the cap, and never a healthy round;
* the QUESTION — a reply shape of its own, with `--ask`'s echo and
  two-verdicts-in-one-reply refusals inherited and its TALLY deliberately not;
* the THREE PROPERTIES, which are #507's own review criteria — a proposal is not
  a finding, disagreement is the signal, and it cannot make a review look cleaner
  than it is;
* the REACH — that the block reaches the payload and the PR comment, under the
  veto lines rather than over them, and that a repo which switched it off is told
  so rather than being left indistinguishable from one where nothing fired.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import panel  # noqa: E402
import panel_propose  # noqa: E402
import panel_rounds  # noqa: E402
import panel_seats  # noqa: E402

DEFAULT_BLOCK = harness_rules.DEFAULTS["review_panel"]


def _finding(seat="claude", severity="P2", title="boom", file="a.py", line=1,
             verdict="confirmed"):
    reported = [panel.Finding(seat, severity, file, line, title, "")]
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=line,
                           synthesis=title, verdict=verdict, reported_by=reported)


def _stop(**over):
    """A `round_stop`-shaped verdict with every rung quiet, for one key to be moved."""
    verdict = {"stop": True, "reason": "dry", "confident": True, "veto": [],
               "fix_injection": {"over": False, "fired": False},
               "new_findings_not_falling": {"over": False, "fired": False},
               "premises": {"repeated": [], "undecidable": [],
                            "undecidable_brake": False}}
    verdict.update(over)
    return verdict


# ------------------------------------------------------------------------ the trigger

def test_every_built_escalate_on_rung_is_one_this_pass_follows():
    """All four, not the three #507 names. `premise_undecidable` (#491) is the same
    kind of event — an `escalate_on` rung, ending the cycle, not as convergence, with
    a human at the veto line — and it is the rung where the fixer has most obviously
    been guessing, since every fix for an unobservable property is an approximation of
    it. A rule covering "some escalations" is one a reader has to memorise the
    membership of."""
    built = {k for k in DEFAULT_BLOCK["escalate_on"]
             if k not in panel_rounds.ESCALATE_ON_UNBUILT}
    assert built == set(panel_propose.PROPOSE_ESCALATIONS)


@pytest.mark.parametrize("rung, verdict", [
    ("fix_injection", _stop(fix_injection={"over": True, "fired": True})),
    ("new_findings_not_falling",
     _stop(new_findings_not_falling={"over": True, "fired": True})),
    ("premise_repeated",
     _stop(premises={"repeated": [{"text": "the cache is warm"}], "undecidable": [],
                     "undecidable_brake": False})),
    ("premise_undecidable",
     _stop(premises={"repeated": [], "undecidable": [{"text": "it is atomic"}],
                     "undecidable_brake": True})),
])
def test_each_rung_that_fired_is_reported_as_fired(rung, verdict):
    assert panel_propose.escalations_fired(verdict) == [rung]


def test_a_measurement_that_merely_CROSSED_is_not_an_escalation():
    """`over` and `fired` are different questions and `round_stop` keeps them apart
    precisely so that "the cycle ended on divergence" cannot be attached to a
    confident, converged round. Reading `over` here would fan a panel's worth of
    tokens out over a round that stopped clean — the below-floor policy stop, the
    round holding an escalation, the round going again under rule 2 for a P1."""
    assert panel_propose.escalations_fired(
        _stop(fix_injection={"over": True, "fired": False},
              new_findings_not_falling={"over": True, "fired": False})) == []


def test_an_undecidable_declaration_the_repo_did_not_ARM_is_not_an_escalation():
    """`premise_state` lists a `decidable: no` declaration whether or not the brake is
    armed — the payload records what the cycle SAID — so the arming has to be checked
    here rather than inferred from the list being non-empty, exactly as `round_stop`
    checks it. A repo that switched #491 off asked for its fixers to be allowed to
    approximate; billing it for a fan-out over a policy it declined applies the policy
    anyway by the back door."""
    assert panel_propose.escalations_fired(
        _stop(premises={"repeated": [], "undecidable": [{"text": "it is atomic"}],
                        "undecidable_brake": False})) == []


def test_a_healthy_round_and_a_missing_verdict_ask_nobody():
    """The cost argument, and the whole of it: this fires on a PR whose cycle was
    already ending badly and buys nothing on a healthy round."""
    assert panel_propose.escalations_fired(_stop()) == []
    assert panel_propose.escalations_fired(None) == []
    assert panel_propose.escalations_fired("stopped") == []


def test_the_ROUND_CAP_is_not_an_escalation():
    """A cap is a COST bound: it ends healthy cycles and diverging ones in the same
    place, and it is what ends most rounds this harness runs. Firing on it would be
    the "every round" this feature exists to avoid, dressed as a rule."""
    capped = _stop(stop=True, confident=False,
                   reason="round cap (2) reached — 3 P1/P2 still outstanding, unreviewed")
    assert panel_propose.escalations_fired(capped) == []


def test_the_rungs_are_reported_in_the_order_round_stop_applies_them():
    both = _stop(fix_injection={"over": True, "fired": True},
                 new_findings_not_falling={"over": True, "fired": True})
    assert panel_propose.escalations_fired(both) == ["new_findings_not_falling",
                                                     "fix_injection"]


# --------------------------------------------------------------------------- the dial

def test_the_default_is_on_and_is_the_one_the_rules_file_documents():
    """On by default, and the properties that earn it are not the brakes'. Those two
    had to argue they could not end a cycle early; this one cannot end a cycle at
    all — it runs after the verdict is final and writes to none of it."""
    assert DEFAULT_BLOCK["propose_on_escalation"] is True


def test_it_is_not_a_fifth_escalate_on_rung():
    """Every key in `escalate_on` answers one question — does this end the cycle? This
    one ends nothing, extends nothing and moves no verdict; it decides what an
    escalation ARRIVES WITH. Filed inside that block it would read as a fifth brake to
    every consumer that enumerates them."""
    assert "propose_on_escalation" not in DEFAULT_BLOCK["escalate_on"]


def test_a_repo_can_switch_it_off_in_one_line():
    notes = []
    assert panel.panel_flag({"propose_on_escalation": False}, "propose_on_escalation",
                            True, notes) is False
    assert panel.panel_flag({"propose_on_escalation": "off"}, "propose_on_escalation",
                            True, notes) is False
    assert notes == []


def test_a_value_that_is_not_a_flag_is_refused_rather_than_guessed():
    with pytest.raises(SystemExit):
        panel.panel_flag({"propose_on_escalation": "sometimes"},
                         "propose_on_escalation", True, [])


def test_it_is_a_board_settable_dial_and_a_flag():
    """Both directions cost something real and neither is a merge policy: ON spends a
    fan-out on cycles that escalate, OFF sends a human to a veto line with no
    proposal. That is exactly the decision a settings channel exists for."""
    dial = harness_rules.BOARD_DIALS["review_panel.propose_on_escalation"]
    assert (dial.kind, dial.nullable, dial.rule) == ("flag", False, "either")


def test_a_repo_that_switched_it_off_is_TOLD_when_a_round_escalates():
    """A governance answer that changes nothing must not be indistinguishable from one
    that was never given — `ESCALATE_ON_UNBUILT`'s rule, applied to the off switch. The
    block says so in its own `reason`, and `panel.run` puts it in `config_notes`, which
    is what a human reads off the PR comment."""
    off = panel_propose.propose(
        _stop(fix_injection={"over": True, "fired": True}), [_finding()],
        ["claude"], {"claude": "sonnet"}, {}, armed=False)
    assert off["asked"] is False
    assert "propose_on_escalation" in off["reason"]


# ------------------------------------------------------------------------ the question

@pytest.mark.parametrize("said, want", [
    ("change", "change"),
    ("no small change", "no small change"),
    ("no_small_change", "no small change"),
    ("NO SMALL CHANGE", "no small change"),
    ("cannot tell", "cannot tell"),
    ("cant tell", "cannot tell"),
])
def test_the_spellings_a_model_reaches_for_are_read_as_the_answer_they_are(said, want):
    got = panel_propose.parse_proposal(
        '{"verdict": "%s", "proposal": "narrow the guard"}' % said)
    assert got is not None and got.verdict == want


def test_the_schema_handed_straight_back_is_not_an_answer():
    """`_ask_verdict`'s whole echo defence, and it works here for its reason: the
    illustration is spelled as the union of the legal values, so it is not one of
    them. A seat that quoted the prompt has not answered."""
    assert panel_propose.parse_proposal(
        '{"verdict": "change|no small change|cannot tell", "proposal": "one line"}'
    ) is None


def test_a_reply_carrying_a_findings_array_is_a_review_and_not_an_answer():
    """The seat was asked what it would DO. A reply that came back with findings is an
    answer to a question nobody asked — and, more to the point, is the one shape that
    could put a proposal pass's output where findings go."""
    assert panel_propose.parse_proposal(
        '{"verdict": "change", "proposal": "x", "findings": [{"severity": "P1"}]}'
    ) is None


def test_two_different_verdicts_in_one_reply_are_not_an_answer():
    """Nothing here is willing to choose which the model meant — `parse_answer`'s
    refusal, inherited whole, including the one INSIDE a single object that
    `json.loads` would otherwise settle by keeping the last key."""
    assert panel_propose.parse_proposal(
        '{"verdict": "change", "proposal": "a"} {"verdict": "cannot tell", '
        '"proposal": "b"}') is None
    assert panel_propose.parse_proposal(
        '{"verdict": "change", "verdict": "no small change", "proposal": "a"}') is None


def test_two_AGREEING_candidates_resolve_to_the_last_of_them():
    """A model that restates its answer at the end of a reply has restated it, and the
    wording there is the one it settled on."""
    got = panel_propose.parse_proposal(
        'thinking… {"verdict": "change", "proposal": "first"} '
        'so: {"verdict": "change", "proposal": "second"}')
    assert got.proposal == "second"


def test_an_unreadable_reply_is_not_the_seat_saying_cannot_tell():
    """One is a seat that looked and could not settle it; the other is a seat whose
    answer we do not have. Folding the second into the first would put a proposal pass
    on record as having been answered by a seat that said nothing."""
    assert panel_propose.parse_proposal("I would refactor the module.") is None
    assert panel_propose.parse_proposal("") is None


def test_the_prompt_tells_the_seat_that_proposing_does_not_make_it_right():
    """Property 1 said to the SEAT, not only enforced behind it. A reviewer that
    believed this was scored would propose to score, which is the second-author
    failure #507 says the finding contract exists to prevent."""
    body = panel_propose.PROPOSE_PROMPT
    assert "not scored" in body
    assert "proposing does not make you right" in body
    assert "SMALLEST change" in body


def test_no_small_change_is_a_first_class_answer_and_not_an_absence():
    """The answer this feature is FOR as much as `change` is: a seat saying its
    findings have no joint small resolution is the same information property 2 gets
    from four incompatible proposals, arriving cheaper."""
    assert "no small change" in panel_propose.PROPOSE_VERDICTS
    got = panel_propose.parse_proposal(
        '{"verdict": "no small change", "proposal": "the escalation ladder wants '
        'inverting", "resolves": []}')
    assert got.verdict == "no small change" and got.proposal


# ---------------------------------------------------------- property 1: not a finding

def test_a_proposal_never_becomes_a_finding():
    """It produces no `Canonical`, so there is nothing for the leaderboard, the
    cross-round defect chain or the severity floors to receive. #79's
    answer-versus-panel distinction is the precedent and here it is structural rather
    than a rule anyone has to remember."""
    block = _asked({"claude": ("change", "narrow the guard", ["F1"])},
                   [_finding()])
    assert isinstance(block, dict)
    seat = block["seats"]["claude"]
    assert "severity" not in seat and "verdict" in seat
    assert seat["verdict"] in panel_propose.PROPOSE_VERDICTS
    assert not any(isinstance(v, panel.Canonical)
                   for v in _flatten(block))


def test_the_block_rides_beside_round_stop_and_never_inside_it():
    """A consumer reading the verdict must not have to step over a proposal to reach
    it, and `round_stop`'s keys are what an orchestrator's `jq` acts on."""
    assert "proposals" in panel._payload_defaults()
    assert "proposals" not in (panel._payload_defaults()["round_stop"] or {})


def test_a_dismissed_finding_is_never_put_to_a_seat():
    """The judge already ruled it not real. Asking a seat what it would change to
    resolve a finding the panel refused is asking it to argue the dismissal, which is
    a second bite at a verdict this pass has no standing to reopen."""
    dismissed = _finding(title="not a bug", verdict="dismissed")
    assert panel_propose.seat_findings([dismissed]) == {}


def test_sonarqube_is_not_a_seat_on_this_pass():
    """It scans code against a rule set and has no reply to give — `panel_ask` says so
    about the identical case. Its gate issues go unrepresented and do not go
    unenforced: a red gate keeps the PR unmergeable whatever anyone proposes."""
    gate = panel.Canonical(id="34-F02", severity="P3", file="a.py", line=2,
                           synthesis="python:S1481", verdict="sonar",
                           reported_by=[panel.Finding("sonarqube", "P3", "a.py", 2,
                                                      "python:S1481", "")])
    assert "sonarqube" not in panel_propose.seat_findings([gate])


# ------------------------------------------------------ property 2: disagreement wins

def test_four_incompatible_proposals_are_all_reported_and_none_is_chosen():
    """The most useful possible answer on a stuck cycle: it says the finding set has no
    small resolution, which is the thing nobody currently learns until round five. A
    verdict struck over them would average away the one thing worth collecting."""
    block = _asked({
        "claude": ("change", "delete the branch", ["F1"]),
        "codex": ("change", "keep the branch and widen the guard", ["F1"]),
        "pi": ("no small change", "the ladder wants inverting", []),
        "antigravity": ("cannot tell", "I would need the caller", []),
    }, [_finding(seat=s) for s in ("claude", "codex", "pi", "antigravity")])
    assert len(block["seats"]) == 4
    assert {s["proposal"] for s in block["seats"].values()} == {
        "delete the branch", "keep the branch and widen the guard",
        "the ladder wants inverting", "I would need the caller"}
    # No tally: nothing here reduces four answers to one.
    assert "verdict" not in block and "agreement" not in block


def test_the_counts_describe_the_fan_out_and_do_not_adjudicate_it():
    """`counts` is the split a reader most wants and cannot get from prose. It is still
    not a verdict — two seats saying `change` have not agreed on a change, and this
    file never claims they have."""
    block = _asked({"claude": ("change", "a", []), "codex": ("change", "b", []),
                    "pi": ("no small change", "c", [])},
                   [_finding(seat=s) for s in ("claude", "codex", "pi")])
    assert block["counts"] == {"change": 2, "no small change": 1, "cannot tell": 0}
    lines = "\n".join(panel_propose.propose_lines(block))
    assert "delete" not in lines  # nothing invented
    assert "2 of 3 agree" not in lines and "consensus" not in lines


def test_the_report_says_out_loud_that_disagreement_IS_the_answer():
    block = _asked({"claude": ("change", "a", [])}, [_finding()])
    lines = "\n".join(panel_propose.propose_lines(block))
    assert "A proposal is not a finding" in lines
    assert "no small resolution" in lines


# ------------------------------------- property 3: it cannot make a review look clean

def test_the_pass_does_not_touch_the_verdict_it_was_handed():
    """The whole of property 3, asserted rather than argued: `stop`, `reason`, `veto`
    and `confident` go in and come out byte-identical. It is pure ADDITION to an
    escalation that has already been decided."""
    import copy
    verdict = _stop(stop=True, confident=False, reason="the count is not coming down",
                    veto=["the new-finding count has not fallen"],
                    new_findings_not_falling={"over": True, "fired": True})
    before = copy.deepcopy(verdict)
    _asked({"claude": ("change", "a", [])}, [_finding()], stop=verdict)
    assert verdict == before


def test_the_report_section_goes_UNDER_the_veto_lines_and_not_over_them():
    """A reader meets what ended the cycle first and what the seats would do about it
    second. A plan at the top of an escalation is exactly the "cleaner than it is" this
    must not be able to produce — so the ordering is pinned in the file rather than
    left to whoever next edits the render."""
    body = Path(panel.__file__).read_text()
    veto = body.index('for why in stop["veto"]:')
    section = body.index("panel_propose.propose_lines(proposals)")
    assert veto < section


def test_a_seat_that_could_not_be_run_is_a_stated_absence_and_not_a_silence():
    """A stated skip is the panel's idiom for a seat that could not be run, and it
    keeps the absence in the report rather than in a shorter list of proposals that
    reads like agreement."""
    block = _asked({"claude": ("change", "a", [])}, [_finding(seat=s)
                                                    for s in ("claude", "codex")],
                   skips={"codex": "codex (gpt-5): CLI absent"})
    lines = "\n".join(panel_propose.propose_lines(block))
    assert "did not answer" in lines and "CLI absent" in lines


def test_one_seat_raising_never_takes_the_ROUND_down_with_it():
    """`run_seat` does filesystem work, and ENOSPC or a permission error on any of it
    raises outside the err-string path. Re-raised here it would lose a whole round's
    report over a block that is additional information about a verdict already taken.
    That trade is not close."""
    def boom(name, *a, **kw):
        if name == "codex":
            raise OSError("no space left on device")
        return panel_propose.SeatProposal("change", "a", "", [])

    block = _run_with(boom, [_finding(seat=s) for s in ("claude", "codex")])
    assert block["asked"] is True
    assert "OSError" in block["seats"]["codex"]["skip"]
    assert block["seats"]["claude"]["verdict"] == "change"


# ---------------------------------------------------------------------- the fan-out

def test_only_the_seats_that_still_have_outstanding_findings_are_asked():
    """#507's own wording. A seat with nothing outstanding has nothing to propose
    about, and asking it would be asking for a review."""
    findings = [_finding(seat="claude"), _finding(seat="codex", title="other",
                                                 file="b.py")]
    by_seat = panel_propose.seat_findings(findings)
    assert set(by_seat) == {"claude", "codex"}
    block = _asked({"claude": ("change", "a", [])}, findings,
                   selected=["claude", "pi"])
    assert set(block["seats"]) == {"claude"}


def test_a_defect_three_seats_raised_is_in_all_three_lists():
    """Attribution is a FIELD (#79) and never an inference from a merge that threw the
    evidence away. Each of them made that finding; each is asked about its own."""
    shared = panel.Canonical(
        id="34-F03", severity="P1", file="a.py", line=9, synthesis="the same defect",
        verdict="confirmed",
        reported_by=[panel.Finding(s, "P1", "a.py", 9, "the same defect", "")
                     for s in ("claude", "codex", "pi")])
    by_seat = panel_propose.seat_findings([shared])
    assert set(by_seat) == {"claude", "codex", "pi"}


def test_an_escalated_finding_is_shown_and_MARKED_as_the_humans_to_answer():
    """It is outstanding, and the human at the veto line is exactly who needs a
    proposal on it — an escalated finding is one the fixer has already said the
    approach is wrong on. What it must NOT become is a licence for a fix pass to patch
    one, so the brief says whose answer it is and `round_stop`'s subtraction is
    untouched."""
    held = frozenset({_finding().key})
    listing, mapped, cut = panel_propose._finding_listing([_finding()], held, "claude")
    assert "ESCALATED" in listing and "no fix round may touch" in listing
    assert mapped[0]["escalated"] is True


def test_the_labels_are_ordinals_so_a_transposed_digest_cannot_pass_for_an_answer():
    """A finding key is a hex digest: a model echoing one back is one transposed
    character from naming a finding that does not exist, and nothing could tell that
    from a real answer. An ordinal it can copy, and the map back lives in the payload
    beside the answers."""
    listing, mapped, _ = panel_propose._finding_listing(
        [_finding(), _finding(title="other", file="b.py")], frozenset(), "claude")
    assert "[F1]" in listing and "[F2]" in listing
    assert [m["label"] for m in mapped] == ["F1", "F2"]
    assert all(len(m["key"]) > 4 for m in mapped)


def test_a_label_naming_nothing_it_was_shown_is_RECORDED_not_dropped():
    """A model inventing an `F9` over a one-finding listing is a fact about how well
    this prompt is being followed, and this is the only place it can be seen."""
    block = _asked({"claude": ("change", "a", ["F1", "F9"])}, [_finding()])
    seat = block["seats"]["claude"]
    assert seat["resolves"] == [_finding().key]
    assert seat["unmatched"] == ["F9"]
    assert "not a finding it was shown" in "\n".join(panel_propose.propose_lines(block))


def test_a_seat_shown_only_SOME_of_its_findings_says_so():
    """A proposal made over 20 of a seat's 31 findings is a different claim from one
    made over all 31, and a reader has to be able to tell. Said in the block's notes
    and in the report, never silent."""
    many = [_finding(title=f"boom {n}", file=f"f{n}.py")
            for n in range(panel_propose.PROPOSE_MAX_FINDINGS + 5)]
    block = _asked({"claude": ("no small change", "too many", [])}, many)
    assert block["seats"]["claude"]["findings_cut"] == 5
    lines = "\n".join(panel_propose.propose_lines(block))
    assert f"{panel_propose.PROPOSE_MAX_FINDINGS} of " \
           f"{panel_propose.PROPOSE_MAX_FINDINGS + 5} finding(s)" in lines


def test_an_escalation_with_no_seat_to_ask_is_reported_rather_than_looking_asked():
    """"We did not ask" and "we asked and nobody answered" are different claims, and a
    consumer forced to tell them apart from an empty `seats` would be reading the
    payload's age rather than the round's state."""
    block = panel_propose.propose(
        _stop(fix_injection={"over": True, "fired": True}), [], ["claude"],
        {"claude": "sonnet"}, {})
    assert block["asked"] is False
    assert "no seat" in block["reason"]
    assert block["escalations"] == ["fix_injection"]


def test_a_review_only_run_has_no_escalation_to_attach_to():
    block = panel_propose.propose(None, [_finding()], ["claude"],
                                  {"claude": "sonnet"}, {}, cycle_run=False)
    assert block["asked"] is False and "no cycle ran" in block["reason"]


def test_the_block_has_the_same_shape_on_every_exit():
    """`_ask_payload_defaults`' lesson, inherited: the exit a consumer is least likely
    to have tested against is the one that ran nothing, and it was the one written
    short."""
    ran = _asked({"claude": ("change", "a", [])}, [_finding()])
    for skipped in (panel_propose.propose(None, [], [], {}, {}, cycle_run=False),
                    panel_propose.propose(_stop(), [_finding()], ["claude"],
                                          {"claude": "sonnet"}, {}),
                    panel._payload_defaults()["proposals"]):
        assert set(skipped) == set(ran), skipped


# ------------------------------------------------------------------------- the reach

def test_the_report_says_nothing_at_all_on_a_round_that_did_not_escalate():
    """Most rounds. A heading saying nothing was asked would be a paragraph about the
    absence of a paragraph, and the escalation's own veto lines are the output where
    there is one."""
    assert panel_propose.propose_lines(
        panel_propose.propose(_stop(), [_finding()], ["claude"],
                              {"claude": "sonnet"}, {})) == []
    assert panel_propose.propose_lines(panel._payload_defaults()["proposals"]) == []


def test_the_section_names_the_rung_that_fired_and_attributes_every_proposal():
    """In front of whoever the escalation goes to, which is #507's third clause. The
    rung is named because "the cycle stopped" and "the cycle stopped because the count
    stopped falling" send a reader to different places."""
    block = _asked({"claude": ("change", "narrow the guard to the fix range", ["F1"])},
                   [_finding()],
                   stop=_stop(new_findings_not_falling={"over": True, "fired": True}))
    lines = "\n".join(panel_propose.propose_lines(block))
    assert "`new_findings_not_falling`" in lines
    assert "**claude**" in lines and "narrow the guard to the fix range" in lines


def test_the_round_reads_the_dial_before_a_single_seat_is_dispatched():
    """The three brakes beside it are read at the same moment and for the same reason:
    a malformed value has to hard-exit before a panel has been paid for, not after."""
    body = Path(panel.__file__).read_text()
    assert body.index('panel_flag(panel, "propose_on_escalation"') < body.index(
        "panel_propose.propose(")


# ------------------------------------------------------------------------- machinery

def _flatten(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _flatten(v)
    elif isinstance(value, list):
        for v in value:
            yield from _flatten(v)
    else:
        yield value


def _run_with(fake, findings, stop=None, selected=None):
    """`propose` with the seat call replaced — the fan-out is the part a suite must
    not pay for, and it is the one part `run_seat` already has its own tests for."""
    real = panel_propose.propose_llm
    panel_propose.propose_llm = fake
    try:
        seats = selected or list(panel.LLM_REVIEWERS)
        return panel_propose.propose(
            stop or _stop(fix_injection={"over": True, "fired": True}),
            findings, seats,
            {n: "a-model" for n in panel.LLM_REVIEWERS}, {})
    finally:
        panel_propose.propose_llm = real


def _asked(answers, findings, stop=None, selected=None, skips=None):
    """One seat answer per entry of `answers`, as `(verdict, proposal, resolves)`."""
    skips = skips or {}

    def fake(name, model, prompt, effort=""):
        if name in skips:
            return panel_propose.SeatProposal(skip=skips[name])
        verdict, proposal, resolves = answers[name]
        return panel_propose.SeatProposal(verdict, proposal, "", list(resolves))

    return _run_with(fake, findings, stop=stop, selected=selected)


# ------------------------------------------- what a codex second opinion found here

def test_a_seat_is_shown_what_IT_WROTE_and_not_the_judges_merge_of_it():
    """Codex's second finding on this PR, and it is the difference between the
    question working and not. `Canonical.synthesis` is the judge's merged statement
    over every reporter of one defect, so a finding three seats raised carries one
    sentence none of them wrote — and the whole premise of this pass is *given these
    findings of YOURS*. A seat asked to propose against a rewording it does not
    recognise is being asked about somebody else's finding."""
    shared = panel.Canonical(
        id="34-F04", severity="P1", file="a.py", line=9,
        synthesis="the judge's merged wording", verdict="confirmed",
        reported_by=[
            panel.Finding("claude", "P1", "a.py", 9, "claude's own title",
                          "and claude's own detail"),
            panel.Finding("codex", "P1", "a.py", 11, "codex's own title", "")])
    mine, _, _ = panel_propose._finding_listing([shared], frozenset(), "claude")
    assert "claude's own title" in mine and "and claude's own detail" in mine
    assert "codex's own title" not in mine
    theirs, mapped, _ = panel_propose._finding_listing([shared], frozenset(), "codex")
    assert "codex's own title" in theirs and "claude's own title" not in theirs
    # ...and its OWN location, which is what it wrote and need not be the merge's.
    assert "a.py:11" in theirs
    assert mapped[0]["line"] == 11


def test_the_merged_wording_is_shown_BESIDE_it_where_the_two_differ():
    """It is what the PR comment and the next round call this defect, so a seat
    proposing against a wording the report does not use would be proposing about a
    finding nobody can find. Dropped where they are the same — the ordinary unmerged
    case — rather than printing one sentence twice."""
    merged = panel.Canonical(
        id="34-F05", severity="P2", file="a.py", line=1, synthesis="the merge",
        verdict="confirmed",
        reported_by=[panel.Finding("claude", "P2", "a.py", 1, "mine", "")])
    listing, _, _ = panel_propose._finding_listing([merged], frozenset(), "claude")
    assert "the panel merged this" in listing and "the merge" in listing
    plain, _, _ = panel_propose._finding_listing([_finding()], frozenset(), "claude")
    assert "the panel merged this" not in plain


def test_a_CHANGE_verdict_with_no_change_in_it_is_not_an_answer():
    """Codex's third finding. The whole content of that verdict IS the proposal:
    without it the reply says a small change exists and does not say what it is,
    which is the criticism-without-a-proposal this feature exists to remove, now
    wearing the feature's own label. Unreadable rather than recorded, which is what
    buys `run_seat`'s one retry."""
    for empty in ('{"verdict": "change", "proposal": ""}',
                  '{"verdict": "change"}',
                  '{"verdict": "change", "proposal": "   "}',
                  '{"verdict": "no small change", "proposal": ""}'):
        assert panel_propose.parse_proposal(empty) is None, empty


def test_cannot_tell_is_complete_on_its_own():
    """`--ask`'s precedent that a bare verdict is still a verdict. Refusing it would
    push a seat that genuinely cannot tell into inventing a sentence for the retry."""
    got = panel_propose.parse_proposal('{"verdict": "cannot tell"}')
    assert got is not None and got.verdict == "cannot tell" and got.proposal == ""


def test_this_pass_gives_no_seat_a_code_tree_and_so_takes_no_code_budget():
    """Codex's first finding, answered rather than dismissed. `reviewer_code_budget_usd`
    is the CODE-READING seat's per-invocation cap and `claude_args` emits
    `--max-budget-usd` only under `reads_code` — "a diff-only seat makes one call with
    a bounded prompt, so a cap there adds a way to LOSE the seat and buys nothing".
    This pass hands out no checkout, so there is no code-reading seat for the cap to
    apply to, and a seat lost to one would be a proposal missing from an escalation."""
    seen = {}

    def spy(cmd_name, model, prompt, effort="", parse=None, code_tree=None,
            budget_usd=None):
        seen.update(code_tree=code_tree, budget_usd=budget_usd)
        return panel_seats.SeatTurn(reply='{"verdict": "cannot tell"}',
                                    parsed=panel_propose.Proposal(
                                        "cannot tell", "", "", []))

    real = panel_propose.run_seat
    panel_propose.run_seat = spy
    try:
        panel_propose.propose_llm("claude", "sonnet", "prompt")
    finally:
        panel_propose.run_seat = real
    assert seen == {"code_tree": None, "budget_usd": None}
