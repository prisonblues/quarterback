"""#789's second half: a shadowed premise rung must not refuse the fix either.

#779 gave every brake a mode and #789 wired the two premise rungs into it — at the
ROUND, where `round_stop` reads `fired` rather than a list. It stopped at the more
important half. Both brakes are evaluated twice, and #84 and #491 both say the
declaration is where it counts: at the round a stop is "one whole fix pass and one
whole panel too late", while `panel.py --premise` refuses the patch before it is
written.

`declare_premise` read no mode at all, so `escalate_modes.premise_repeated: shadow`
shadowed the round's stop and the fixer's declaration was **still refused with exit
4**. A brake half in shadow is worse than one not in shadow at all, because the
operator has been told it is recording.

So this file pins the interface, which is the exit code, plus the two things that
have to move with it:

* **Exit 0 under shadow, exit 4 under enforce**, on both rungs and on both together.
  A caller reads exit 4 as *do not write this fix*; a shadowed rung producing it is
  the whole defect.
* **Recorded, and said plainly.** Shadow is not silence: the occurrence still lands
  in the register, the verdict is published in the payload under the four siblings'
  vocabulary, and the report says the fix WOULD have been refused. A calibration
  population nobody can see being collected is not worth collecting.
* **No board write.** A `needs-human` row parks the work as surely as an exit code
  does, so a shadowed rung must not announce one either.
* **The two `config_notes` at the round**, which looped over the RECORD lists and
  said "the cycle ends here and a human answers the premise" whatever the mode was.
  Gated on `fired` now, which is the same discipline the `fix_injection` note twenty
  lines below already applies.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_rounds  # noqa: E402
from test_panel_dials import PANEL_CFG  # noqa: E402

LANDED = "a local repository can say where a release number landed"
KEY_A = "aaaaaaaaaaaaaaaa"
KEY_B = "bbbbbbbbbbbbbbbb"

REPEATED = panel_rounds.BRAKED_RUNGS["premises.repeated_verdict"]
UNDECIDABLE = panel_rounds.BRAKED_RUNGS["premises.undecidable_verdict"]


def cfg(modes=None, **panel_keys):
    """`PANEL_CFG` with a `review_panel` block, and `escalate_modes` when a test asks.

    Written as a REPO SETTING rather than injected as an argument, because that is
    the seam an operator actually has: the mode is resolved from the rules file
    `declare()` already loads, so a test that handed the mode in directly would prove
    nothing about the path a repo takes."""
    block = dict(panel_keys)
    if modes is not None:
        block["escalate_modes"] = modes
    return {**PANEL_CFG, "review_panel": block}


@pytest.fixture
def repo(monkeypatch):
    def use(config=None):
        monkeypatch.setattr(panel_rounds, "load_repo_cfg", lambda name: config or cfg())
    use()
    return use


def declare(register, premise, round_no, *keys, decidable="unknown"):
    return panel.declare("board", premise, str(register), round_no, list(keys), 34,
                         False, decidable)


def register(path):
    return json.loads(Path(path).read_text())


# ---- the exit code, which is the whole interface ---------------------------


def test_a_shadowed_repeat_records_the_verdict_and_permits_the_fix(
        repo, tmp_path, capsys):
    """The defect, red before #789's second half. Round 2's declaration reaches the
    same verdict it always did and the fixer is allowed to write the patch — because
    the operator asked for this rung to record rather than act, and shadowing the
    round's stop while still refusing the pass is the worst of both."""
    repo(cfg(modes={REPEATED: "shadow"}))
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A) == 0
    assert declare(reg, LANDED, 2, KEY_B) == 0, \
        "a shadowed rung refused the fix with exit 4"

    out = capsys.readouterr().out
    assert "SHADOW" in out and "WOULD HAVE REFUSED" in out
    assert "STOP — DO NOT WRITE THIS FIX." not in out
    assert f"`escalate_on.{REPEATED}`" in out
    # Recorded, not skipped: shadow is a mode on the ACTION, never on the count, or
    # the rung could never be armed off its own calibration population.
    entry = register(reg)["premises"][0]
    assert entry["rounds"] == [1, 2]


def test_the_same_declaration_under_enforce_still_refuses(repo, tmp_path):
    """The other arm, so the test above cannot pass by breaking the brake. `enforce`
    is what `DEFAULTS` names for both premise rungs, so this is also the shipped
    posture."""
    repo(cfg(modes={REPEATED: "enforce"}))
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A) == 0
    assert declare(reg, LANDED, 2, KEY_B) == panel_rounds.PREMISE_REPEATED_EXIT


def test_a_repo_that_names_no_mode_at_all_is_refused_exactly_as_before(
        repo, tmp_path):
    """`escalate_mode`'s middle layer, reached through this path. A repo that wrote
    no `escalate_modes` block leaves every rung absent, and reading absence as
    `shadow` would disarm six brakes on the upgrade that added the parameter."""
    repo(cfg())
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A) == 0
    assert declare(reg, LANDED, 2, KEY_B) == panel_rounds.PREMISE_REPEATED_EXIT


def test_shadowing_one_premise_rung_does_not_disarm_the_other(repo, tmp_path):
    """The failure `escalate_mode`'s three layers exist to prevent, in the one place
    both premise rungs are read at once: `review_panel` merges one level deep, so a
    repo naming one rung leaves the other ABSENT — and absent is the DEFAULT, not
    shadow."""
    repo(cfg(modes={REPEATED: "shadow"},
             escalate_on={"premise_undecidable": True}))
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A, decidable="no") == \
        panel_rounds.PREMISE_REPEATED_EXIT


def test_a_shadowed_undecidable_answer_permits_the_first_fix(repo, tmp_path, capsys):
    """#491's rung is the one that refuses a fix on its FIRST declaration, which is
    the thing the occurrence counter structurally cannot do — so it is also the one
    where a wrongly-armed shadow costs the most, and the one an operator is most
    likely to calibrate before arming."""
    repo(cfg(modes={UNDECIDABLE: "shadow"},
             escalate_on={"premise_undecidable": True}))
    reg = tmp_path / "premises.json"
    assert declare(reg, LANDED, 1, KEY_A, decidable="no") == 0
    out = capsys.readouterr().out
    assert "SHADOW" in out and f"`escalate_on.{UNDECIDABLE}`" in out
    # The answer is STICKY and is recorded whatever the mode did with it: a property
    # the runtime cannot observe does not become observable because a rung was
    # shadowed.
    assert register(reg)["premises"][0]["decidable"] == "no"


def test_a_shadowed_rung_announces_nothing_to_the_board(repo, tmp_path, monkeypatch):
    """A `needs-human` row parks the work as surely as an exit code does, so the
    board write is gated on the APPLIED verdict rather than on the measurement. A
    shadowed rung that still filed a blocker would be enforcing by another route."""
    calls: list = []
    monkeypatch.setattr(panel_rounds, "announce_escalation",
                        lambda *a, **k: calls.append(a) or "announced")
    repo(cfg(modes={REPEATED: "shadow"}))
    reg = tmp_path / "premises.json"
    declare(reg, LANDED, 1, KEY_A)
    declare(reg, LANDED, 2, KEY_B)
    assert calls == [], "a shadowed brake parked the work on a human's queue"


# ---- the verdict, published in the four siblings' vocabulary ---------------


def test_the_declaration_publishes_would_fire_beside_fired(repo, tmp_path, capsys):
    """The calibration pair, in the shape `fix_injection` publishes it, so a consumer
    that can read one rung can read this one. Asserted on the `--json` payload rather
    than on the prose, because that is what an orchestrator reads."""
    repo(cfg(modes={REPEATED: "shadow"}))
    reg = tmp_path / "premises.json"
    panel.declare("board", LANDED, str(reg), 1, [KEY_A], 34, True, "unknown")
    capsys.readouterr()
    panel.declare("board", LANDED, str(reg), 2, [KEY_B], 34, True, "unknown")
    got = json.loads(capsys.readouterr().out)

    assert got["repeated"] is True, "the measurement moved with the mode"
    assert got["would_escalate"] is True and got["escalate"] is False
    v = got["repeated_verdict"]
    assert v["over"] is True and v["armed"] is True
    assert v["mode"] == "shadow" and v["would_fire"] is True and v["fired"] is False
    # The rung that was not asked about reads as the shipped posture, not as shadow.
    assert got["undecidable_verdict"]["mode"] == "enforce"


def test_the_reason_says_it_would_have_refused(repo, tmp_path, capsys):
    """A reason that reads exactly like the enforcing one under a command that exited
    0 is how a fixer learns to ignore the block entirely. The sentence carries both
    halves — the verdict, and the fact that it was applied to nothing."""
    repo(cfg(modes={REPEATED: "shadow"}))
    reg = tmp_path / "premises.json"
    panel.declare("board", LANDED, str(reg), 1, [KEY_A], 34, True, "unknown")
    capsys.readouterr()
    panel.declare("board", LANDED, str(reg), 2, [KEY_B], 34, True, "unknown")
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert reason.startswith("WOULD HAVE REFUSED THIS FIX, and did not:")
    assert "premise declared 2 time(s)" in reason
    assert "shadow" in reason


# ---- the round's two notes -------------------------------------------------


def stop_with(modes, **over):
    """`round_stop` for a cycle whose register holds one twice-declared premise."""
    premises = {"limit": 2, "declared": 1, "wired": True, "stamped": 0,
                "retroactive": [], "undeclared_rounds": [],
                "undecidable_brake": True,
                "repeated": [{"key": "p1", "text": LANDED, "rounds": [1, 2]}],
                "undecidable": [{"key": "p1", "text": LANDED, "rounds": [1, 2]}]}
    premises["repeated_verdict"] = panel_rounds.premise_repeated_state(premises, modes)
    premises["undecidable_verdict"] = panel_rounds.premise_undecidable_state(
        premises, modes)
    return {**over, "premises": premises}


@pytest.mark.parametrize("mode,expect", [("enforce", True), ("shadow", False)])
def test_the_round_notes_are_gated_on_fired_not_on_the_record(monkeypatch, mode,
                                                              expect):
    """The two `config_notes` looped over `premises["repeated"]` and
    `premises["undecidable"]` — the RECORD lists, populated whatever the mode is —
    and said the cycle ends here and a human answers the premise. Under shadow the
    cycle did not end there, and this line is published as a public PR comment under
    `--post`: a note contradicting the `reason` beside it is worse than no note.

    Asserted against the two verdict blocks the notes now read, which is the same
    `fired`-not-`over` discipline the `fix_injection` note below them documents.
    """
    modes = {REPEATED: mode, UNDECIDABLE: mode}
    stop = stop_with(modes)
    assert stop["premises"]["repeated_verdict"]["would_fire"] is True
    assert stop["premises"]["repeated_verdict"]["fired"] is expect
    assert stop["premises"]["undecidable_verdict"]["fired"] is expect

    notes: list[str] = []
    # The gate as `run()` spells it, asserted here rather than through a whole round:
    # the two lines are a straight read of the block above, and a round would take
    # eighty lines of stubs to reach them.
    if stop["premises"]["repeated_verdict"]["fired"]:
        notes.append("the cycle ends here and a human answers the premise")
    if stop["premises"]["undecidable_verdict"]["fired"]:
        notes.append("every fix for it is an approximation")
    assert bool(notes) is expect


def test_the_run_gate_reads_the_verdict_blocks_and_not_the_lists():
    """The seam, read off the source. The test above spells the gate; this pins that
    `run()` spells it the same way, because a copy asserts on the copy — and the
    failure here is silent by construction, a true sentence in a public comment about
    a cycle that did not end.
    """
    src = (Path(__file__).resolve().parent.parent / "panel.py").read_text()
    assert 'if stop["premises"]["repeated_verdict"]["fired"]:' in src
    assert 'if stop["premises"]["undecidable_verdict"]["fired"]:' in src
    # ...and the pre-#789 spellings are gone, both of them. The repeat loop is
    # matched at its OLD indent — it still exists, one level in from where it was,
    # and asserting its absence outright would fail on the gated version.
    assert '\n    for repeated in stop["premises"]["repeated"]:' not in src
    assert 'if premise_undecidable else []' not in src
