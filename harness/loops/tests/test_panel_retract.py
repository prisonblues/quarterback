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


# ---- through run(): the three defects a second opinion found -----------------
#
# All three were in the first draft of this feature, none was caught by the suite
# above, and each is pinned here because that is the only reason it stays fixed.

import json  # noqa: E402
import panel  # noqa: E402
import panel_core  # noqa: E402
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/tmp/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}


def _stub(monkeypatch, findings):
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
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
           declined=(), retract=(), name="r", max_rounds=5):
    _stub(monkeypatch, findings)
    out = tmp_path / f"{name}{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline),
                     max_rounds=max_rounds, scope="pr",
                     declined=list(declined), retract=list(retract)) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def _p2():
    return panel.Finding("claude", "P2", "a.py", 3, "unvalidated input", "")


def test_the_payload_keeps_the_retracted_declination_on_record(monkeypatch, capsys,
                                                               tmp_path):
    """The first draft emitted the register AFTER subtracting, so the next round
    inherited a retraction whose declination was gone — nothing matched, and the
    "names no declination" note fired falsely for the rest of the cycle. History
    goes in the payload; the subtraction is a reading of it."""
    _, r1, p1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = next(iter(r1["declined"]), None) or list(r1["to_fix"])[0]["key"]
    _, r2, p2 = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                       baseline=[p1], declined=[f"{key}:budget"], name="d")
    assert key in r2["declined"], "the declaration must be on record"

    _, r3, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=3,
                      baseline=[p2], retract=[key], name="x")
    assert key in r3["declined"], "the record must survive the retraction"
    assert key in r3["retracted"], "and the retraction must travel too"
    assert r3["round_stop"]["declined_outstanding"] == [], \
        "but it must no longer be outstanding"


def test_a_retraction_lifts_the_hold_it_was_aimed_at(monkeypatch, capsys, tmp_path):
    _, r1, p1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = list(r1["to_fix"])[0]["key"]
    _, _, p2 = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                      baseline=[p1], declined=[f"{key}:budget"], name="d")
    _, held, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=3,
                        baseline=[p2], name="h")
    assert held["round_stop"]["declined_outstanding"] == [key]

    _, freed, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=3,
                         baseline=[p2], retract=[key], name="f")
    assert freed["round_stop"]["declined_outstanding"] == []


def test_a_declination_made_after_the_retraction_stands(monkeypatch, capsys,
                                                        tmp_path):
    """Recurrence is real (#508). A key retracted in an earlier round and declined
    AFRESH in a later one is a new assertion about a defect that came back, not the
    old declaration returning — and the first draft discarded it silently."""
    _, r1, p1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = list(r1["to_fix"])[0]["key"]
    _, _, p2 = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                      baseline=[p1], declined=[f"{key}:budget"], name="d")
    _, r3, p3 = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=3,
                       baseline=[p2], retract=[key], name="x")
    assert r3["round_stop"]["declined_outstanding"] == []

    _, r4, _ = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=4,
                      baseline=[p3], declined=[f"{key}:refuted"], name="again")
    assert r4["round_stop"]["declined_outstanding"] == [key], \
        "a fresh declaration must outlive the retraction that preceded it"


def test_a_skipped_round_carries_the_retraction_through(monkeypatch, capsys,
                                                        tmp_path):
    """The defect that broke the invariant outright. A round that reviews nothing
    builds its payload on its own path, and the first draft rebuilt `declined`
    there from the baseline while emitting no `retracted` at all — so a retraction
    made earlier VANISHED at the quietest round of the cycle, the veto came back,
    and the next round held the PR out of a strict landing on a declination a
    human had already answered.

    A declaration cannot be recorded by a round that reviewed nothing, because it
    is dated to the round that made it. A retraction can: it is an act about a key
    that already exists, and it needs no findings to be true."""
    _, r1, p1 = _round(monkeypatch, capsys, tmp_path, [_p2()])
    key = list(r1["to_fix"])[0]["key"]
    _, _, p2 = _round(monkeypatch, capsys, tmp_path, [_p2()], round_no=2,
                      baseline=[p1], declined=[f"{key}:budget"], name="d")

    # a round that reviews nothing. The skip is config-driven — a title matching
    # `skip_title_patterns` — and getting that wrong is how the first version of
    # this test passed against the very bug it was written to catch.
    skip_cfg = {**CFG, "review_panel": {"skip_title_patterns": ["^Merge "]}}
    _stub(monkeypatch, [_p2()])
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: skip_cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "Merge branch 'main' into feature", "additions": 3,
              "deletions": 1, "headRefName": "h", "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n",
        compare='{"status": "ahead", "files": [{"filename": "a.py", "patch": "@@"}]}'))
    out = tmp_path / "skip3.json"
    panel.run("board", 34, post=False, json_file=str(out), record=False,
              round_no=3, baseline=[p2], max_rounds=5, scope="pr",
              retract=[key])
    capsys.readouterr()
    skipped = json.loads(out.read_text())
    assert skipped.get("reviewed") is False, (
        "this test is worthless unless the round actually skipped — check "
        "skip_title_patterns")
    assert key in (skipped.get("retracted") or {}), (
        "a retraction passed to a round that reviewed nothing must survive it — "
        "losing it resurrects the veto that holds the PR")
