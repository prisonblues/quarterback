"""#84's futility brake: stop when the rounds have stopped being about different things.

The round cap bounds COST — N rounds and stop, whatever is happening. Nothing bounded
FUTILITY, and PR #299 is what that costs. Five rounds. Rounds 1, 2 and 3 each found
the PREVIOUS round's fix reopening the same hole, patched three different ways — merge
parents, then same-named refs, then a purely local branch — and the premise underneath
all three, *that a local repository can say where a release number LANDED*, was named
only at round 3, by the orchestrating human, and answered by deleting the machinery.
39 of the 53 findings after round 1 were introduced by the previous fix pass; round 2
was 17 out of 17.

So the rule, and it is a count of OCCURRENCES rather than of rounds: the second time a
fix is written against a premise the previous round invalidated, stop. Not the third.
Evaluated when a fix is PROPOSED — `panel.py --premise`, before the fix pass runs — and
not when a round completes, which is one whole fix pass and one whole panel too late.

What this suite pins is the three things that make it a mechanism rather than a note:

* the COUNT — one premise declared twice is two occurrences, and one declared twice in
  the same round is one;
* the STOP — the second declaration exits non-zero and says which findings to escalate
  instead, and a repeat that reaches a round anyway ends the cycle there and is never
  reported as convergence;
* the GAP — a fix pass that declared no premise is UNESCALATABLE and says so, because
  #84's instruction is to report that rather than infer a premise from the findings,
  and a cycle nobody could have braked reads exactly like one that did not need it.

The limits are pinned too, deliberately: two different PROXIES for one premise share
almost no words and are counted as two premises. That is not a bug to be fixed with a
similarity heuristic — #84 rules that out in as many words — it is the reason the brake
is a declaration, and a test that asserts it is what stops the gap being forgotten.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_rounds  # noqa: E402
import panel_seats  # noqa: E402
import harness_rules  # noqa: E402
from test_panel_dials import PANEL_CFG, stub  # noqa: E402

#: The premise #299 circled for three rounds, in the words a fixer would have used.
LANDED = "a local repository can say where a release number landed"

#: Two of the three proxies that premise wore. Textually unrelated; identical in what
#: they assume. See `test_two_proxies_for_one_premise_are_two_premises_and_that_is_the
#: _known_limit`.
PROXY_MERGE = "a merge parent proves the number reached the default branch"
PROXY_REFS = "a ref of the same name proves the number reached the default branch"

KEY_A = "aaaaaaaaaaaaaaaa"
KEY_B = "bbbbbbbbbbbbbbbb"


def cfg(escalate_on=..., **panel_keys):
    """`PANEL_CFG` with a `review_panel` block, `escalate_on` included unless a test
    asks for it to be absent."""
    block = dict(panel_keys)
    if escalate_on is not ...:
        block["escalate_on"] = escalate_on
    return {**PANEL_CFG, "review_panel": block}


@pytest.fixture
def repo(monkeypatch):
    """`declare()` resolves the repo's rules like any other entry point. One knob:
    the test hands back whatever config it wants read."""
    def use(config=None):
        monkeypatch.setattr(panel_rounds, "load_repo_cfg",
                            lambda name: config or cfg())
    use()
    return use


def declare(register, premise, round_no, *keys, pr=34, json_out=False,
            decidable="unknown"):
    return panel.declare("board", premise, str(register), round_no, list(keys), pr,
                         json_out, decidable)


def register(path):
    return json.loads(Path(path).read_text())


# ------------------------------------------------------------------ the count and stop

def test_the_second_fix_against_one_premise_is_refused_so_the_third_round_never_runs(
        repo, tmp_path, capsys):
    """#299's shape, and the whole issue in one test.

    Round 1 finds the defect and fix 1 is written against premise P. Round 2 finds fix
    1 reopened the same hole, and fix 2 would be written against P again — that is the
    SECOND occurrence, and it is where the cycle stops. Round 3, which on #299 found
    exactly the same thing a third way, never runs at all."""
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A) == 0
    out = capsys.readouterr().out
    assert "occurrence 1 of 2" in out and "write the fix" in out

    assert declare(reg, LANDED, 2, KEY_B) == panel_rounds.PREMISE_REPEATED_EXIT
    out = capsys.readouterr().out
    assert "STOP — DO NOT WRITE THIS FIX." in out
    # The keys the next round must not count as work a fix pass can clear — which is
    # how the brake reaches the stop rule instead of growing a second one.
    assert f"--escalated {KEY_A} --escalated {KEY_B}" in out


def test_the_declaration_that_was_refused_is_still_recorded(repo, tmp_path):
    """What #84 counts is DECLARATIONS, so the log has to hold the one that was stopped
    as well as the ones that were allowed — otherwise a caller that ignores the exit
    code and writes the fix anyway leaves a register that says it never proposed it."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A)
    declare(reg, LANDED, 2, KEY_B)
    entry, = register(reg)["premises"]
    assert entry["rounds"] == [1, 2]
    assert entry["findings"] == [KEY_A, KEY_B]


def test_one_premise_declared_twice_in_one_round_is_one_occurrence(repo, tmp_path):
    """A fixer that states its premise, is interrupted and states it again has proposed
    ONE fix pass. Counting the restatement would fire the brake on a cycle that never
    circled, which is the false positive that teaches a fixer never to declare one."""
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 2, KEY_A) == 0
    assert declare(reg, LANDED, 2, KEY_B) == 0
    assert register(reg)["premises"][0]["rounds"] == [2]


def test_a_restatement_in_the_same_words_is_the_same_premise(repo, tmp_path):
    """Case, punctuation and spacing are not a new premise. The register keys on the
    words (`_norm_title`), the same normalisation a finding key is built from."""
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A) == 0
    assert declare(reg, "  A local REPOSITORY can say, where a release number landed!  ",
                   2, KEY_B) == panel_rounds.PREMISE_REPEATED_EXIT


def test_a_reworded_restatement_is_matched_and_the_earlier_wording_is_shown(
        repo, tmp_path, capsys):
    """A plural is a rewording, not a new premise — the rule `Baseline.raised_before`
    already applies to a reworded finding title, reused rather than reinvented. The
    report names the wording it matched, because a match a human cannot check is a stop
    they cannot argue with.

    Its reach is exactly that rule's and no further: a high character ratio AND the same
    content words. "repo" for "repository" is a different word to it, and so is "zero"
    for "0" — which is the reason the briefs ask for the premise restated rather than
    reworded, and the reason the test below pins the proxy case as a known limit rather
    than as a bug."""
    reg = tmp_path / "premises.json"
    declare(reg, "a local checkout knows which release numbers landed", 1, KEY_A)
    capsys.readouterr()
    assert declare(reg, "a local checkout knows which release number landed",
                   2, KEY_B) == panel_rounds.PREMISE_REPEATED_EXIT
    assert "restates" in capsys.readouterr().out


def test_two_proxies_for_one_premise_are_still_two_premises_to_the_COUNTER(
        repo, tmp_path):
    """The gap in the COUNT, pinned so it cannot be forgotten or quietly closed with a
    similarity heuristic.

    #62's three proxies were `rc == 0`, an artefact's existence and a head SHA moving:
    textually unrelated, identical in what they assumed. #84 rules out inferring the
    premise from the findings and says to compare DECLARATIONS — so a fixer that
    declares the proxy instead of the premise defeats the counter, and the answer is
    NOT a similarity rule this file would then have to calibrate.

    What changed with #491 is the answer, not this fact: the counter still sees two
    premises here, and the brake that catches this cycle is the decidability question
    put to each declaration on its own. See
    `test_the_undecidable_answer_catches_what_the_counter_cannot`, which is this same
    scenario with the question answered."""
    reg = tmp_path / "premises.json"
    assert declare(reg, PROXY_MERGE, 1, KEY_A) == 0
    assert declare(reg, PROXY_REFS, 2, KEY_B) == 0
    assert len(register(reg)["premises"]) == 2


# ------------------------------------------------- #491: the undecidable declaration

def test_the_undecidable_answer_catches_what_the_counter_cannot(repo, tmp_path):
    """The whole point, in one test: the exact scenario the counter is blind to, with
    the decidability question answered.

    Two proxies for one unobservable property, honestly declared, sharing almost no
    words — `test_two_proxies_for_one_premise_are_still_two_premises_to_the_COUNTER`
    is this without the answer, and it runs to two clean declarations. Answered, the
    FIRST one is refused, so the second fix pass never happens."""
    reg = tmp_path / "premises.json"
    assert declare(reg, PROXY_MERGE, 1, KEY_A,
                   decidable="no") == panel_rounds.PREMISE_REPEATED_EXIT


def test_it_fires_on_the_first_declaration_not_the_second(repo, tmp_path):
    """The asymmetry with `premise_repeated`, pinned because it looks like an
    inconsistency and is not. An unobservable property does not become observable on
    the next attempt, so a second occurrence confirms nothing the first did not say —
    at the price of a fix pass and a whole panel."""
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A, decidable="no") == \
        panel_rounds.PREMISE_REPEATED_EXIT
    assert register(reg)["premises"][0]["rounds"] == [1]


def test_a_decidable_yes_is_recorded_and_brakes_nothing(repo, tmp_path):
    """The flag is not a switch that stops everything it touches. A fixer that looked
    at the question and found the property observable writes the fix."""
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A, decidable="yes") == 0
    assert register(reg)["premises"][0]["decidable"] == "yes"


def test_a_declaration_that_was_never_asked_the_question_brakes_nothing(repo, tmp_path):
    """`unknown` is the honest default: every declaration written before #491 existed
    reads this way, and #84's rule for an undeclared fix pass is the same rule one
    level down — report the gap, never guess at it."""
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A) == 0
    assert register(reg)["premises"][0]["decidable"] == "unknown"


def test_the_refused_declaration_is_still_recorded(repo, tmp_path):
    """`declare_premise`'s existing rule, which the new brake must not quietly break:
    the register holds the declaration that was STOPPED as well as the ones allowed,
    or the round-side half has nothing to read."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A, decidable="no")
    entry = register(reg)["premises"][0]
    assert entry["rounds"] == [1] and entry["decidable"] == "no"
    assert entry["findings"] == [KEY_A]


def test_the_answer_survives_a_reload_or_the_round_cannot_read_it(repo, tmp_path):
    """`load_premises` rebuilds every entry field by field, so an answer it drops is an
    answer the round-side half never sees — the brake would then fire before the fix
    and be silent afterwards."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A, decidable="no")
    loaded, problems = panel_rounds.load_premises(str(reg), "acme/board", 34)
    assert not problems
    assert loaded["premises"][0]["decidable"] == "no"


def test_silence_does_not_retract_an_earlier_answer(repo, tmp_path):
    """A later pass that simply did not pass the flag has not un-established what an
    earlier one answered — and the brake still fires, because it reads the ENTRY and
    not the declaration in front of it. Reading the silence as a retraction would let
    a caller clear the brake by dropping an argument."""
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A,
                   decidable="no") == panel_rounds.PREMISE_REPEATED_EXIT
    assert declare(reg, LANDED, 2, KEY_B) == panel_rounds.PREMISE_REPEATED_EXIT
    assert register(reg)["premises"][0]["decidable"] == "no"


def test_a_later_yes_does_not_clear_an_earlier_no(repo, tmp_path):
    """The hole every self-reported signal in this loop has: the agent whose fix is
    being refused supplies the answer. Without this, a fixer stopped on `no`
    re-declares the same premise with `yes` and the refusal is gone with nothing
    recording that it happened — the actor lifting its own brake by changing what it
    says, which is what `round_stop`'s docstring says cannot be self-reported.

    `no` is established about the PROPERTY. A property the runtime cannot observe does
    not become observable because a later declaration says otherwise."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A, decidable="no")
    assert declare(reg, LANDED, 2, KEY_B,
                   decidable="yes") == panel_rounds.PREMISE_REPEATED_EXIT
    assert register(reg)["premises"][0]["decidable"] == "no"


def test_a_yes_is_recorded_freely_until_a_no_lands(repo, tmp_path):
    """Stickiness runs one way only. The ordinary case — a fixer answering honestly,
    round after round — stays exactly as cheap as it was, or the flag would become
    something a fixer learns not to answer."""
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A, decidable="yes") == 0
    assert register(reg)["premises"][0]["decidable"] == "yes"
    declare(reg, LANDED, 2, KEY_B, decidable="no")
    assert register(reg)["premises"][0]["decidable"] == "no"


def test_an_answer_on_disk_that_nothing_recognises_reads_as_unknown(tmp_path):
    """A register a later harness (or a hand edit) wrote must not stop the cycle. It
    degrades to the value that never brakes, and the rest of the entry is kept —
    unlike a bad ARGUMENT, which is a caller to correct and raises."""
    reg = tmp_path / "premises.json"
    reg.write_text(json.dumps({"version": 1, "repo": "board", "pr": 34, "premises": [
        {"text": LANDED, "rounds": [1], "findings": [KEY_A], "decidable": "maybe"}]}))
    loaded, problems = panel_rounds.load_premises(str(reg), "board", 34)
    assert not problems
    assert loaded["premises"][0]["decidable"] == "unknown"
    assert loaded["premises"][0]["rounds"] == [1]


def test_a_bad_answer_in_an_ARGUMENT_is_refused_rather_than_read_as_unknown():
    """The other side of the line above. A typo coerced to `unknown` is a brake that
    does not fire on a declaration that answered `no`, which is this mechanism failing
    in the exact direction it exists to prevent."""
    with pytest.raises(ValueError) as e:
        panel_rounds.declare_premise({}, LANDED, 1, [KEY_A], 2, "No!", True)
    assert "decidable" in str(e.value)


# --------------------------------------------------------------- #491: the dial

def test_the_ask_path_refuses_the_answer_rather_than_ignoring_it(monkeypatch, tmp_path):
    """The rule `--premise-for` is refused by, and its reason: a flag accepted and
    ignored is a caller believing it asked for something this run does not do. `--ask`
    puts a premise to the SEATS; it declares nothing, so there is no register for an
    answer to be recorded in."""
    monkeypatch.setattr(sys, "argv", ["panel.py", "--ask", "a premise",
                                      "--premise-decidable", "no"])
    with pytest.raises(SystemExit) as e:
        panel.main()
    assert "--premise-decidable" in str(e.value)


def test_a_review_round_refuses_the_answer_too(monkeypatch):
    """A round writes no fix, so it has none to answer for. It reads the answers
    already in the register through `--premise-file`."""
    monkeypatch.setattr(sys, "argv", ["panel.py", "--pr", "1",
                                      "--premise-decidable", "no"])
    with pytest.raises(SystemExit) as e:
        panel.main()
    assert "--premise-decidable belongs to --premise" in str(e.value)


def test_the_undecidable_brake_ships_on(repo):
    """On by default, like `premise_repeated` and unlike the rest of #78's table: it
    can only fire on a fixer's own explicit `no`, which cannot happen by accident, and
    its output is "stop and ask a human"."""
    assert harness_rules.DEFAULTS["review_panel"]["escalate_on"] == {
        "premise_repeated": 2, "premise_undecidable": True, "fix_injection": 0.5,
        "new_findings_not_falling": 1, "unrefereed_fix": True,
        "guard_lines": False}
    assert panel_rounds.premise_undecidable_brake(
        harness_rules.DEFAULTS["review_panel"], []) is True


def test_a_repo_that_wrote_only_the_other_key_keeps_this_one(repo, tmp_path):
    """`review_panel` merges one level deep, so a written `escalate_on` REPLACES the
    default object. Without the per-key fallback, `{"premise_repeated": 2}` would
    silently switch this brake off — which is the exact failure #84 hit and is not
    worth shipping twice."""
    assert panel_rounds.premise_undecidable_brake(
        {"escalate_on": {"premise_repeated": 2}}, []) is True


def test_switching_it_off_lets_the_fix_be_written(repo, tmp_path):
    """A repo may decide it would rather a fixer approximate than stop."""
    repo(cfg(escalate_on={"premise_undecidable": False}))
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A, decidable="no") == 0


def test_switching_it_off_still_records_the_answer_and_says_so(repo, tmp_path, capsys):
    """`ESCALATE_ON_UNBUILT`'s rule: a governance answer that changes nothing must not
    be indistinguishable from one that was never given."""
    repo(cfg(escalate_on={"premise_undecidable": False}))
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A, decidable="no")
    assert register(reg)["premises"][0]["decidable"] == "no"
    assert "premise_undecidable` is off" in capsys.readouterr().out


@pytest.mark.parametrize("value", [2, "yes", 0.5, []])
def test_a_number_here_is_refused_because_there_is_nothing_to_count(value):
    """`2` would mean "approximate it once first", which is the behaviour the brake
    exists to refuse. The value it reads is a fixer's yes/no about one property, so
    there is no occurrence to count."""
    with pytest.raises(SystemExit) as e:
        panel_rounds.premise_undecidable_brake(
            {"escalate_on": {"premise_undecidable": value}}, [])
    assert "escalate_on.premise_undecidable" in str(e.value)


def test_the_new_key_is_not_reported_as_a_typo():
    assert "review_panel.escalate_on" not in harness_rules.unknown_keys(
        {"review_panel": {"escalate_on": {"premise_undecidable": True}}})


# ------------------------------------------- #491: the late half, at the round

def test_an_undecidable_premise_that_reaches_a_round_ends_the_cycle():
    """The same shape as the repeat's late half, and for the same reason: `--premise`
    refuses the fix when it is PROPOSED, and a caller that ignored exit 4 wrote it
    anyway. The register is then the record that says so."""
    reg = {"premises": [{"key": "p1", "text": LANDED, "norm": LANDED, "rounds": [1],
                         "findings": [KEY_A], "decidable": "no"}]}
    # A new finding, so absent the brake this round would GO AGAIN — the stop then
    # means the brake, not rule 4's "otherwise dry".
    stop = panel_rounds.round_stop(
        2, 5, ["k1"], [], [], premises=panel_rounds.premise_state(reg, 2, 2, True))
    assert stop["stop"] is True
    assert "cannot observe" in stop["reason"]


def test_the_undecidable_stop_is_never_reported_as_convergence():
    """A cycle ending on an open question is not a clean finish, and a reader who
    cannot tell the two apart has been told the opposite of the truth. Named for the
    brake it covers: #84's repeat has a test of this name already, and two of them
    would mean only the second ever ran."""
    reg = {"premises": [{"key": "p1", "text": LANDED, "norm": LANDED, "rounds": [1],
                         "findings": [KEY_A], "decidable": "no"}]}
    stop = panel_rounds.round_stop(
        2, 5, ["k1"], [], [], premises=panel_rounds.premise_state(reg, 2, 2, True))
    assert stop["confident"] is False
    assert any("#491" in v for v in stop["veto"])


def test_a_repo_that_disarmed_the_brake_does_not_get_its_cycle_ended():
    """`premise_state` lists the declaration either way — the payload records what the
    cycle SAID — so the arming check has to happen at the stop rather than being
    inferred from the list being non-empty."""
    reg = {"premises": [{"key": "p1", "text": LANDED, "norm": LANDED, "rounds": [1],
                         "findings": [KEY_A], "decidable": "no"}]}
    state = panel_rounds.premise_state(reg, 2, 2, False)
    assert state["undecidable"] and state["undecidable_brake"] is False
    assert panel_rounds.round_stop(2, 5, ["k1"], [], [], premises=state)["stop"] is False


def test_the_payload_records_the_declaration_and_the_arming_separately():
    """Collapsing the two would make a repo that switched the brake off
    indistinguishable from one where no fixer ever answered the question."""
    reg = {"premises": [{"key": "p1", "text": LANDED, "norm": LANDED, "rounds": [1],
                         "findings": [KEY_A], "decidable": "no"}]}
    stop = panel_rounds.round_stop(
        2, 5, ["k1"], [], [], premises=panel_rounds.premise_state(reg, 2, 2, False))
    assert [p["key"] for p in stop["premises"]["undecidable"]] == ["p1"]
    assert stop["premises"]["undecidable_brake"] is False


# ------------------------------------------------------- #491: what the fixer reads

def test_the_question_is_put_to_every_declaration_not_only_the_braking_one(
        repo, tmp_path, capsys):
    """A fixer that has never seen the question does not know it was asked. The line
    that only appears when it stops you is the line nobody reads until it is too late
    to have answered."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A)
    assert "--premise-decidable" in capsys.readouterr().out


def test_the_stop_explains_the_brake_that_actually_fired(repo, tmp_path, capsys):
    """#84's sentence tells a fixer not to patch the same premise again. To a fixer
    stopped on its FIRST declaration that is simply untrue about its own cycle, and a
    stop whose explanation does not match what happened is one a caller argues with."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A, decidable="no")
    out = capsys.readouterr().out
    assert "STOP — DO NOT WRITE THIS FIX." in out
    assert "not decidable where the assertion runs" in out
    assert "fix pass 1 against one premise the previous round invalidated" not in out


def test_a_declaration_that_trips_both_brakes_says_both(repo, tmp_path):
    """They are different questions and a fixer acting on one has not answered the
    other."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A, decidable="yes")
    verdict = panel_rounds.declare_premise(
        panel_rounds.load_premises(str(reg))[0], LANDED, 2, [KEY_A], 2, "no", True)
    assert verdict["repeated"] and verdict["undecidable"]
    assert "NOT decidable" in verdict["reason"]
    assert "declared 2 time(s)" in verdict["reason"]


def test_a_third_occurrence_still_stops_rather_than_passing_the_dial(repo, tmp_path):
    """The dial is the occurrence it is stopped ON, not one it is compared to for
    equality: a caller that ignored the brake once must not find it switched off."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A)
    declare(reg, LANDED, 2, KEY_A)
    assert declare(reg, LANDED, 3, KEY_A) == panel_rounds.PREMISE_REPEATED_EXIT


# ------------------------------------------------------------------------ the dial

def test_the_default_is_the_one_the_rules_file_documents():
    """Two occurrences — "the second time" — and it is ON by default, unlike the rest of
    #78's table. It can only fire after a premise has been DECLARED twice, which cannot
    happen by accident, and its output is "stop and ask a human".

    `fix_injection` is the block's second implemented matter (#489) and is asserted
    here rather than only in its own suite, because what this test is really pinning
    is that the SHIPPED block and the reader agree — and a block that grew a key the
    exact-equality would otherwise pass over is how a default drifts away from the
    file that documents it."""
    assert harness_rules.DEFAULTS["review_panel"]["escalate_on"] == {
        "premise_repeated": 2, "premise_undecidable": True, "fix_injection": 0.5,
        "new_findings_not_falling": 1, "unrefereed_fix": True,
        "guard_lines": False}
    assert panel_rounds.premise_repeat_limit(
        harness_rules.DEFAULTS["review_panel"], []) == 2


def test_a_repo_that_never_heard_of_the_key_gets_the_default():
    assert panel_rounds.premise_repeat_limit({}, []) == 2


def test_null_switches_the_brake_off(repo, tmp_path):
    """How a repo asks for the pre-#84 behaviour: the declarations are still recorded,
    and nothing brakes on a repeat."""
    assert panel_rounds.premise_repeat_limit({"escalate_on":
                                              {"premise_repeated": None}}, []) is None
    repo(cfg(escalate_on={"premise_repeated": None}))
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A) == 0
    assert declare(reg, LANDED, 2, KEY_B) == 0
    assert register(reg)["premises"][0]["rounds"] == [1, 2]


def test_a_repo_that_set_a_looser_dial_gets_the_round_it_asked_for(repo, tmp_path):
    """The middle of the dials suite's three failures — a key nothing reads is worse
    than no key, because it reads as configured. At `3` the second declaration is
    allowed and the third is not."""
    repo(cfg(escalate_on={"premise_repeated": 3}))
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A) == 0
    assert declare(reg, LANDED, 2, KEY_A) == 0
    assert declare(reg, LANDED, 3, KEY_A) == panel_rounds.PREMISE_REPEATED_EXIT


def test_a_repo_that_wrote_escalate_on_keeps_the_default_for_what_it_did_not_mention():
    """`review_panel` is merged one level deep, so a written `escalate_on` REPLACES the
    default object. Without the per-key fallback, naming one of #78's other matters
    would silently switch the brake off — a governance setting turning another one off
    by omission."""
    assert panel_rounds.premise_repeat_limit(
        {"escalate_on": {"quorum_failed": True}}, []) == 2


def test_a_reserved_matter_nothing_implements_is_reported_rather_than_dropped():
    """`require_failing_test`'s precedent. A repo that switched on a governance rule and
    saw nothing would reasonably conclude it was in force."""
    notes = []
    panel_rounds.premise_repeat_limit({"escalate_on": {"judge_absent": True}}, notes)
    assert any("`escalate_on.judge_absent`" in n and "NOT enforced" in n for n in notes)


@pytest.mark.parametrize("value", ["two", 0, 1, True, 2.5, [2]])
def test_a_malformed_dial_is_refused_loudly_rather_than_defaulted(value):
    """A malformed value of a key this harness KNOWS is a typo, and applying the default
    anyway runs the cycle under a policy the file did not ask for. `1` is in the list on
    its own merits: it would escalate the FIRST time any premise was declared."""
    with pytest.raises(SystemExit) as e:
        panel_rounds.premise_repeat_limit({"escalate_on":
                                           {"premise_repeated": value}}, [])
    assert "escalate_on.premise_repeated" in str(e.value)


def test_an_escalate_on_that_is_not_an_object_is_refused_too():
    with pytest.raises(SystemExit) as e:
        panel_rounds.premise_repeat_limit({"escalate_on": "premise_repeated"}, [])
    assert "`review_panel.escalate_on`" in str(e.value)


def test_a_typo_inside_escalate_on_is_warned_about_rather_than_silently_dropped():
    """The same sweep every other block gets. `escalate_on: {"premise_repeatd": 5}` would
    otherwise leave the brake at its default with nothing on stderr, on the block that
    decides when a cycle stops asking a fixer to patch one assumption."""
    unknown = harness_rules.unknown_keys(
        {"review_panel": {"escalate_on": {"premise_repeatd": 5}}})
    assert unknown["review_panel.escalate_on"] == ["premise_repeatd"]


def test_the_two_reserved_names_are_not_reported_as_typos():
    """#78 names them, so a repo that wrote one has not made a mistake — it has asked
    for something not built, which is a different answer from "nothing reads that"."""
    assert "review_panel.escalate_on" not in harness_rules.unknown_keys(
        {"review_panel": {"escalate_on": {"quorum_failed": True,
                                          "judge_absent": True}}})


# ------------------------------------------------------------------- the undeclared gap

def test_a_fix_pass_that_declared_nothing_is_named_as_unescalatable(repo, tmp_path,
                                                                    capsys):
    """#84: treat an undeclared fix as unescalatable rather than pretending to infer.
    The point of saying it is that a cycle nobody COULD have braked reads exactly like a
    cycle that did not need braking."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 3, KEY_A)
    out = capsys.readouterr().out
    assert "UNESCALATABLE" in out and "round(s) 1, 2" in out


def test_a_cycle_that_declared_everything_says_nothing_about_gaps(repo, tmp_path,
                                                                  capsys):
    reg = tmp_path / "premises.json"
    declare(reg, PROXY_MERGE, 1, KEY_A)
    declare(reg, PROXY_REFS, 2, KEY_B)
    capsys.readouterr()
    declare(reg, "a third assumption entirely", 3, KEY_A)
    assert "UNESCALATABLE" not in capsys.readouterr().out


def test_round_one_has_no_earlier_fix_pass_to_have_declared_anything():
    assert panel_rounds.undeclared_passes({"premises": []}, 1) == []


# --------------------------------------------------------------- the stop rule's half

def _state(rounds, limit=2, round_no=3):
    reg = panel_rounds.new_premise_register("acme/board", 34)
    for r in rounds:
        panel_rounds.declare_premise(reg, LANDED, r, [KEY_A], limit)
    return panel_rounds.premise_state(reg, round_no, limit)


def test_a_repeated_premise_that_reaches_a_round_anyway_ends_the_cycle():
    """The late half of the brake: worse than stopping before the fix, better than the
    cap. It fires at any of `round_stop`'s four rules — here on a round that had a P1
    outstanding and would otherwise have gone again."""
    blocker = panel.Canonical(id="34-F01", severity="P1", file="a.py", line=1,
                              synthesis="boom", verdict="confirmed",
                              reported_by=[panel.Finding("claude", "P1", "a.py", 1,
                                                         "boom", "")])
    got = panel_rounds.round_stop(2, 5, [], [blocker], [], premises=_state([1, 2]))
    assert got["stop"] is True
    assert "a human answers this, not another fix pass" in got["reason"]
    assert LANDED in got["reason"]


def test_that_stop_is_never_reported_as_convergence():
    got = panel_rounds.round_stop(3, 5, [], [], [], premises=_state([1, 2]))
    assert got["stop"] is True and got["confident"] is False
    assert any("declared more than once" in v for v in got["veto"])


def test_the_premise_stop_is_not_the_cap_and_does_not_say_it_is():
    """Both can be true of one round, and "the rounds stopped being about different
    things" is the more specific truth. A reader told only "cap reached" goes looking
    for a bigger cap."""
    got = panel_rounds.round_stop(3, 3, ["deadbeefdeadbeef"], [], [],
                                  premises=_state([1, 2]))
    assert "round cap" not in got["reason"]
    assert "written against more than once" in got["reason"]


def test_a_declaration_never_buys_another_round():
    """Declarations end a loop; they never extend one. A register is a claim by the
    agent about to write the fix, and the one thing #67 says cannot be self-reported is
    whether the loop is making progress."""
    dry = panel_rounds.round_stop(2, 5, [], [], [], premises=_state([1], round_no=2))
    assert dry["stop"] is True and dry["confident"] is True
    assert dry["premises"]["repeated"] == []


def test_a_round_with_no_register_still_answers_the_question():
    """Always present, so a consumer never has to tell "nothing was declared" from "a
    payload written before the field" — that would be reading a payload's age."""
    got = panel_rounds.round_stop(3, 5, [], [], [])
    assert got["premises"] == {"limit": None, "declared": 0, "repeated": [],
                              "undecidable": [], "undecidable_brake": False,
                              "undeclared_rounds": [1, 2]}


def test_the_stop_records_which_premise_and_which_rounds():
    got = panel_rounds.round_stop(3, 5, [], [], [], premises=_state([1, 2]))
    repeated, = got["premises"]["repeated"]
    assert repeated["rounds"] == [1, 2] and repeated["occurrences"] == 2
    assert repeated["findings"] == [KEY_A]


# ------------------------------------------------------------------- the register file

def test_a_register_that_could_not_be_read_is_reported_not_swallowed(tmp_path):
    """The failure a silent read would cause is the one the brake exists to prevent,
    arriving with nothing said: an unreadable register makes the second occurrence look
    like the first, and the fix gets written."""
    bad = tmp_path / "premises.json"
    bad.write_text("{not json")
    reg, problems = panel_rounds.load_premises(str(bad), "acme/board", 34)
    assert reg["premises"] == []
    assert any("could not be read" in p and "will not fire" in p for p in problems)


def test_a_missing_register_is_not_a_problem(tmp_path):
    """The first declaration of a cycle creates it, and a cycle that never declared one
    is the ordinary undeclared case rather than an error."""
    reg, problems = panel_rounds.load_premises(str(tmp_path / "nope.json"))
    assert (reg["premises"], problems) == ([], [])


def test_a_register_wired_to_another_pr_is_reported_and_not_counted(repo, tmp_path):
    """`load_baseline`'s rule, for its reason: a mis-wired path counts another cycle's
    premises, and it must show up as a reported problem rather than as a brake that
    fires or does not for reasons nobody can see."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A, pr=34)
    loaded, problems = panel_rounds.load_premises(str(reg), "acme/board", 99)
    assert loaded["premises"] == []
    assert any("PR #34, not #99" in p for p in problems)


def test_a_declaration_that_names_no_round_is_not_counted(tmp_path):
    """An occurrence with no round is invisible to `undeclared_passes` and to the count,
    so it is refused rather than dated to a round nobody chose."""
    reg = tmp_path / "premises.json"
    reg.write_text(json.dumps({"premises": [{"text": LANDED, "rounds": []}]}))
    loaded, problems = panel_rounds.load_premises(str(reg))
    assert loaded["premises"] == []
    assert any("names no round" in p for p in problems)


def test_the_register_is_not_written_through_a_symlink(repo, tmp_path):
    """`write_payload`'s `O_NOFOLLOW`, reached through the register: a pre-planted
    symlink at the requested path fails the write instead of following it."""
    target = tmp_path / "secret"
    target.write_text("mine")
    link = tmp_path / "premises.json"
    link.symlink_to(target)
    assert declare(link, LANDED, 1, KEY_A) == panel_rounds.UNWRITTEN_PAYLOAD_EXIT
    assert target.read_text() == "mine"


def test_an_unwritten_register_fails_rather_than_reporting_a_declaration_it_lost(
        repo, tmp_path, capsys):
    """Sharper than the payload's version of the same rule: a lost occurrence makes the
    NEXT declaration of this premise the first, and the brake never fires."""
    assert declare(tmp_path / "no" / "such" / "dir.json", LANDED, 1,
                   KEY_A) == panel_rounds.UNWRITTEN_PAYLOAD_EXIT
    assert "was NOT recorded" in capsys.readouterr().err


# ------------------------------------------------------------------------- the CLI door

def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["panel.py", *argv])
    return panel.main()


def test_an_empty_premise_is_refused(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--premise", "   ", "--premise-file", str(tmp_path / "p"))
    assert "say in one sentence" in str(e.value)


def test_a_declaration_with_nowhere_to_be_counted_is_refused(monkeypatch):
    """The brake counts occurrences across a cycle, so a declaration with no register is
    not a check — it is a print statement that exits 0."""
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--premise", LANDED)
    assert "--premise needs --premise-file" in str(e.value)


def test_premise_for_takes_finding_keys_and_not_ids(monkeypatch, tmp_path):
    """They are what the next round is handed as `--escalated`, and an ID there names no
    finding at all."""
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--premise", LANDED, "--premise-file", str(tmp_path / "p"),
              "--premise-for", "299-F07")
    assert "--premise-for takes finding KEYS" in str(e.value)


def test_the_declaration_path_refuses_the_round_flags(monkeypatch, tmp_path):
    """A declaration is a check made BEFORE a fix pass, not a round: there is nothing to
    post about, no verdict to override, and nothing to compare a baseline against."""
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--premise", LANDED, "--premise-file", str(tmp_path / "p"),
              "--baseline", "/tmp/r1.json")
    assert "--premise does not take --baseline" in str(e.value)


def test_the_two_premise_questions_are_two_commands(monkeypatch, tmp_path):
    """`--ask` puts the premise to the SEATS and costs a vendor call each; `--premise`
    counts how many times a fix has been written against it and costs nothing. Refused
    together rather than ordered, so "which ran?" is never a reading of panel.py's
    branch order."""
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--ask", LANDED, "--premise", LANDED,
              "--premise-file", str(tmp_path / "p"))
    assert "--ask does not take --premise" in str(e.value)


def test_a_round_that_cannot_exist_cannot_hold_a_declaration(monkeypatch, tmp_path):
    """The review path's own check runs after this branch has returned, so a round of
    0 would date the declaration to a round that cannot exist and make
    `undeclared_passes` count from it."""
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--premise", LANDED, "--premise-file", str(tmp_path / "p"),
              "--round", "0")
    assert "rounds are numbered from 1" in str(e.value)


def test_a_review_round_refuses_premise_for(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--pr", "34", "--premise-for", KEY_A)
    assert "--premise-for belongs to --premise" in str(e.value)


def test_the_brake_firing_has_its_own_exit_code(repo, monkeypatch, tmp_path):
    """The caller has to be able to tell the brake FIRING from the command failing to
    run, and both are non-zero. 2 is argparse's usage error and 3 is an unwritten
    payload."""
    reg = str(tmp_path / "premises.json")
    assert _main(monkeypatch, "--repo", "board", "--pr", "34", "--round", "1",
                 "--premise", LANDED, "--premise-file", reg) == 0
    assert _main(monkeypatch, "--repo", "board", "--pr", "34", "--round", "2",
                 "--premise", LANDED, "--premise-file", reg) == 4
    assert panel_rounds.PREMISE_REPEATED_EXIT == 4
    assert panel_rounds.PREMISE_REPEATED_EXIT != panel_rounds.UNWRITTEN_PAYLOAD_EXIT


def test_json_out_gives_the_verdict_a_machine_can_read(repo, monkeypatch, tmp_path,
                                                       capsys):
    reg = str(tmp_path / "premises.json")
    _main(monkeypatch, "--repo", "board", "--pr", "34", "--round", "1",
          "--premise", LANDED, "--premise-file", reg, "--json")
    got = json.loads(capsys.readouterr().out)
    assert got["escalate"] is False and got["occurrence"] == 1
    assert got["key"] == panel_rounds.premise_key(LANDED)


# ------------------------------------------------------------------ through a real round

def _round(monkeypatch, capsys, tmp_path, *, round_no, baseline=(), premise_file="",
           config=None, wrote=None):
    stub(monkeypatch, [], config=config or cfg())
    if wrote is not None:
        real = panel.write_payload
        monkeypatch.setattr(panel, "write_payload",
                            lambda path, payload: (wrote.append(path),
                                                   real(path, payload))[1])
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=5,
                     premise_file=premise_file) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def test_a_round_reads_the_register_and_the_payload_says_what_it_found(
        repo, monkeypatch, capsys, tmp_path):
    """The round is a READER of the register and never a writer — `panel.py --premise` is
    the only writer, because the count is of fix passes PROPOSED and a round proposes
    none. Two writers would be two answers to the one question the brake exists to
    answer."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A)
    declare(reg, LANDED, 2, KEY_A)
    wrote = []
    _, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=3,
                           premise_file=str(reg), wrote=wrote)
    assert str(reg) not in wrote and wrote
    assert payload["round_stop"]["premises"]["repeated"][0]["rounds"] == [1, 2]
    assert payload["round_stop"]["confident"] is False
    assert any("was declared in rounds 1, 2" in n for n in payload["config_notes"])


def test_a_wired_cycle_that_skipped_a_declaration_is_told_so_on_the_pr(
        repo, monkeypatch, capsys, tmp_path):
    """`config_notes` as well as `round_stop`, because the two are read by different
    people at different moments: `jq .round_stop` decides whether to go again, and the
    PR comment is what a human reads afterwards."""
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 2, KEY_A)
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=3,
                                premise_file=str(reg))
    assert any("declared no premise" in n and "round(s) 1" in n
               for n in payload["config_notes"])
    assert "UNESCALATABLE" in report


def test_a_cycle_that_never_wired_the_brake_is_not_nagged_every_round(
        repo, monkeypatch, capsys, tmp_path):
    """A note on every round of every unwired cycle is the "loud and wrong" a reader
    learns to skip, and it would arrive on the same line as the ones that mean
    something. The fact is still in the payload, where an auditor is looking."""
    _, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=3)
    assert not [n for n in payload["config_notes"] if "premise" in n]
    assert payload["round_stop"]["premises"]["undeclared_rounds"] == [1, 2]


# ---------------------------------------------------------------------- the two briefs

REPO_ROOT = Path(__file__).resolve().parents[3]
REVIEW_PR = (REPO_ROOT / "harness/commands/review-pr.md").read_text()
PANEL_REVIEW_PR = (REPO_ROOT / "harness/commands/panel-review-pr.md").read_text()


def test_the_fixers_brief_tells_it_to_declare_before_it_patches():
    """Step 3a is where a premise is already stated in one sentence; #84 is the count and
    the stop around it. A mechanism whose declaration nobody is asked for is #169's
    unwired key with extra steps."""
    assert "--premise-file" in REVIEW_PR
    assert "before you write the patch" in REVIEW_PR


def test_the_orchestrators_brief_runs_the_brake_before_it_re_briefs_a_fix_pass():
    """It is the orchestrator's because it is the only reader with both rounds in front
    of it — the same reason "match it by premise, not by key" is. On #299 the fixers
    escalated zero times across five rounds and the human named the premise."""
    assert "--premise-file /tmp/tmp.AbC123/premises.json" in PANEL_REVIEW_PR
    # The brake still runs BEFORE §4 — that is what this test is named for and it
    # is unchanged. What follows a brake is what #555 changed; see below.
    assert "Read the exit code." in PANEL_REVIEW_PR
    assert "before you go back to §4" in PANEL_REVIEW_PR


def test_a_brake_partitions_the_orchestrators_round_rather_than_dropping_it():
    """#555, and a deliberate reversal of what this brief used to say.

    It read **"Do not launch §4."** — a blanket stop — and this test pinned that
    string. In the panel flow the orchestrator declares the premise (not the
    fixer) and §4 IS "launch the fixer sub-agent", so a brake meant the fixer was
    never launched and the findings the premise says nothing about were fixed by
    nobody. That is the same defect #555 was filed about, one level up and in the
    mirror direction: on lexray#1697 the fixer spent a whole pass on findings the
    premise had voided, and a blanket stop here throws away the ones it had not.

    The rule is that work downstream of an open question is speculative spend — it
    is not that everything alongside such a question is. So the brake now
    partitions: no fix pass for the escalated keys, a fix pass for the rest, and a
    stop after it.
    """
    assert "Do not launch §4." not in PANEL_REVIEW_PR, \
        "the blanket stop is what #555 replaced — see this test's docstring"
    assert "The downstream findings do not get a fix pass." in PANEL_REVIEW_PR
    assert "The independent findings still do." in PANEL_REVIEW_PR
    assert "withheld from the brief" in PANEL_REVIEW_PR
    # The empty case is a real outcome and has to be named, or a reader with
    # nothing independent left invents a pass to justify launching one.
    assert "Unless nothing is left." in PANEL_REVIEW_PR
    # And it must still be a stop: partitioning the pass must not read as licence
    # for another ROUND, which is the brake's whole purpose.
    assert "stop the cycle after this pass" in PANEL_REVIEW_PR


def test_the_fixers_brief_carries_the_fourth_test_and_says_it_stands_alone(
        ):
    """The mechanism is a flag, and a flag nobody is told to answer is #169's unwired key
    with extra steps. The "stands alone" half is load-bearing: as a fourth CONJUNCT it
    could never fire, because test 3 passes precisely when test 4 is failing."""
    assert "decidable in the runtime the assertion runs" in REVIEW_PR
    assert "It is an escalation if tests 1-3 all hold, or if test 4 fails." in REVIEW_PR
    assert "--premise-decidable" in REVIEW_PR


def test_the_orchestrators_brief_asks_for_the_answer_it_is_placed_to_give():
    """A fixer replacing a proxy is answering the finding in front of it, honestly. Only
    the reader holding every round can see that the proxies keep changing while the thing
    being approximated does not — the same argument that makes the declaration itself the
    orchestrator's to run."""
    assert "--premise-decidable" in PANEL_REVIEW_PR
    assert "escalate_on.premise_undecidable" in PANEL_REVIEW_PR


def test_both_briefs_say_the_counter_is_blind_rather_than_leaving_it_to_discipline():
    """"State the premise, never the proxy" is a discipline, and the cycle that produced
    #491 shows what a discipline is worth here: four honest declarations, none matching.
    A brief that offered only the instruction would be promising a detector again."""
    assert "does not depend on your wording" in REVIEW_PR
    assert "restates" in PANEL_REVIEW_PR and "the answer to this flag is" in PANEL_REVIEW_PR


def test_both_briefs_carry_the_limit_rather_than_implying_coverage():
    """The brake compares DECLARATIONS. Two proxies for one premise are two premises, and
    a brief that did not say so would be promising a detector."""
    assert "unescalatable" in REVIEW_PR.lower()
    assert "state the premise, never the proxy" in PANEL_REVIEW_PR


# ------------------------------------------------------------------- the partition


def test_the_escalation_names_both_halves_of_the_partition(repo, tmp_path, capsys):
    """#555. "Fix everything else in the same pass" has been in the brief since
    2026-08-18, and on lexray#1697 round 1 the fixer read it, fixed five findings and
    four of them were about the behaviour of the flag its own escalation questioned.
    The pass was reverted the next day. A sentence is not a mechanism; naming the two
    halves on the screen the fixer is about to act on is one."""
    reg = tmp_path / "p.json"
    declare(reg, LANDED, 1, KEY_A)
    declare(reg, LANDED, 2, KEY_A, KEY_B)
    out = capsys.readouterr().out
    assert "DOWNSTREAM OF THE PREMISE — write no patch for these." in out
    assert KEY_A in out and KEY_B in out
    assert "INDEPENDENT" in out
    assert "it does not end it" in out, "an escalation partitions the pass, not ends it"
    assert "not listed above" in out, "the independent half is defined by subtraction"


def test_a_declaration_that_named_no_findings_says_the_partition_is_missing(
        repo, tmp_path, capsys):
    """The half `declare` cannot compute is the independent one — it never sees the
    round's findings, only the keys this declaration named. So a declaration with no
    keys has stated no partition at all, and the fixer is told that in those words
    rather than being handed a half-empty list it might read as complete."""
    reg = tmp_path / "p.json"
    declare(reg, LANDED, 1)
    declare(reg, LANDED, 2)
    out = capsys.readouterr().out
    assert "NO PARTITION WAS DECLARED" in out
    assert "DOWNSTREAM OF THE PREMISE" not in out


def test_the_partition_rides_in_the_json_as_well_as_the_report(repo, tmp_path, capsys):
    """An orchestrator reading `--json` gets the same two halves the report shows.
    `findings` is the downstream set and has been in the payload all along; what
    #555 adds is that it is now the thing the fixer is told to act on."""
    reg = tmp_path / "p.json"
    declare(reg, LANDED, 1, KEY_A)
    capsys.readouterr()
    declare(reg, LANDED, 2, KEY_A, KEY_B, json_out=True)
    body = json.loads(capsys.readouterr().out)
    assert body["escalate"] is True
    assert sorted(body["findings"]) == sorted([KEY_A, KEY_B])
    assert "board" in body, "what the board did is part of the machine-readable answer"


def test_a_recorded_declaration_reports_no_partition_because_there_is_none(
        repo, tmp_path, capsys):
    """The partition belongs to the escalation. A first occurrence is a fix about to
    be written in full, and printing a do-not-patch list there would tell the fixer
    to skip findings nobody escalated."""
    reg = tmp_path / "p.json"
    declare(reg, LANDED, 1, KEY_A)
    out = capsys.readouterr().out
    assert "DOWNSTREAM OF THE PREMISE" not in out and "NO PARTITION" not in out


def test_the_downstream_list_is_cumulative_and_says_so(repo, tmp_path, capsys):
    """The register UNIONS finding keys across every declaration of a premise
    (`declare_premise`: `entry["findings"] = sorted({*entry["findings"], *keys})`),
    so the escalating verdict carries keys from earlier rounds as well as this
    one. The report must not present that as this round's finding list.

    A second opinion caught this and the first cut of these tests could not have:
    they declared `KEY_A` in both rounds, so the accumulated set and the current
    one were identical and no assertion could tell them apart. Here round 2
    declares only `KEY_B`, and `KEY_A` still has to appear — labelled for what it
    is."""
    reg = tmp_path / "p.json"
    declare(reg, LANDED, 1, KEY_A)
    capsys.readouterr()
    declare(reg, LANDED, 2, KEY_B)
    out = capsys.readouterr().out

    assert KEY_A in out and KEY_B in out, "the list is every key ever declared"
    assert "across round(s) 1, 2" in out, "and it says which rounds it spans"
    assert "cumulative" in out
    assert "must NOT be read as is this round's finding list" in out
    assert "subtract it from your own" in out, "the fixer is told how to use it"


def test_the_independent_half_is_defined_against_this_round_not_the_list(
        repo, tmp_path, capsys):
    """The two halves are drawn from different populations — a cumulative
    do-not-patch set and the current round's outstanding findings — and pairing
    them without saying so is what made the earlier wording wrong."""
    reg = tmp_path / "p.json"
    declare(reg, LANDED, 1, KEY_A)
    declare(reg, LANDED, 2, KEY_B)
    out = capsys.readouterr().out
    assert "every outstanding finding of THIS round that is not listed above" in out
