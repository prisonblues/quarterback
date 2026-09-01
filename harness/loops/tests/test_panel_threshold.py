"""#78's corroboration threshold — how many seats a finding needs before it is work.

`review_panel.threshold_by_severity` is the reviewer-side half of the convergence
problem. Every other brake in this harness acts on a finding that has already been
accepted as work: `low_severity_fix_lines` bounds what a pass may spend on it,
`unrefereed_line_weight` prices where the spending lands, `max_fix_guard_lines`
bounds what one pass may write. This one acts before any of them, and it asks the
question none of them can: should this have become work at all?

**It is also the one dial in the block that can hide a real defect**, which is why
most of this file is about what it may NOT do. A threshold suppresses on a head
count, and a head count is exactly the wrong instrument for the case the panel exists
for — one seat seeing a genuine P1 nobody else spotted. #78's own table has that
finding in it (`32-F01`, solo and real) sitting beside the four solo findings that
were wrong, and #64's round 1 was a panel of ONE, where a threshold of 2 would have
discarded the round entire.

So the bound is in the code and not in the default. `Dials.corroboration_applies`
refuses to apply a threshold to any severity at or above `round_trigger_floor`, or to
`P1`/`P2` at any floor — `panel_core.BLOCKING_SEVERITIES`, which is `round_stop` rule
2's own bar, read from where rule 2 reads it. Two things follow, and both are tested
here rather than asserted in a comment:

* a blocker is handed to the fixer however few seats raised it
  (:func:`test_a_threshold_may_never_stand_down_a_blocker`, and the P2-under-a-P1-floor
  case beside it, which is the one the trigger-floor bound alone gets wrong);
* anything a threshold CAN stand down is a finding `round_stop`'s rules 1, 2 and 3 all
  ignore already, so a stood-down finding cannot hold the cycle open and `round_stop`
  needed no new parameter to learn about this dial
  (:func:`test_a_stood_down_finding_cannot_hold_the_cycle_open`).

The other half is visibility. A finding this stands down keeps its master verdict,
stays in the payload with `below_threshold` and `seats_required` on it, and is printed
under its own heading with the seat count that stood it down — the disposal #165's
below-floor findings already get, for the reason #165 gives: a finding that vanishes
is a finding nobody can argue with.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_rounds  # noqa: E402
import panel_seats  # noqa: E402
import harness_rules  # noqa: E402
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/tmp/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}

#: Two seats, for the corroborated case. `codex` rather than a second claude because
#: `Canonical.reviewers` de-duplicates by member name, so a finding raised twice by
#: one seat is one seat and must not read as agreement.
TWO_SEATS = {"claude": {"enabled": True, "model": "sonnet"},
             "codex": {"enabled": True, "model": "gpt-5"}}


def cfg(*, seats=None, **dials):
    return {**CFG,
            "reviewers": dict(seats or CFG["reviewers"]),
            "review_panel": dict(dials)}


def finding(severity, seat="claude", title="unvalidated input", file="a.py"):
    return panel.Finding(seat, severity, file, 3, title, "")


def _adjudicate(clusters, diff, model, pr, budget=None, coverage=None, cwd=None,
                ci="", **_kw):
    """Every finding confirmed, and findings sharing a title MERGED into one record.

    The merge is the point of this double and the reason `test_panel_dials`' one will
    not do: its `_adjudicate` builds one `Canonical` per reported `Finding`, so every
    record has exactly one reporter and a corroboration threshold has nothing to
    count. Grouping by `(file, title)` is the same identity `cluster_findings` uses as
    its hint and the judge settles for real, and it is what makes `reviewers` — the
    field this whole feature reads — carry more than one name.
    """
    flat = [f for grp in clusters for f in grp]
    groups: dict[tuple, list] = {}
    for f in flat:
        groups.setdefault((f.file, f.title), []).append(f)
    return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=grp[0].severity,
                             file=grp[0].file, line=grp[0].line,
                             synthesis=grp[0].title, verdict="confirmed",
                             detail=grp[0].detail, reported_by=list(grp),
                             rationale="real")
             for i, grp in enumerate(groups.values())], None, panel.CoverageRuling())


def run(monkeypatch, capsys, tmp_path, per_seat, *, config=None, sonar=(),
        round_no=1, baseline=()):
    """One whole panel run. `per_seat` maps a seat name to what that seat filed, so a
    finding can be raised by one member or by two and the tally can tell them apart."""
    resolved = config or CFG
    if sonar:
        resolved = {**resolved,
                    "reviewers": {**resolved["reviewers"],
                                  "sonarqube": {"enabled": True}}}
        monkeypatch.setattr(panel, "review_sonarqube",
                            lambda *a, **k: ("ERROR", list(sonar), [], None))
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: resolved)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "h", "headRefOid": "abc"},
        diff="diff --git a/a.py b/a.py\n+x\n",
        compare='{"status": "ahead", "files": [{"filename": "a.py", "patch": "@@"}]}'))
    monkeypatch.setattr(panel, "review_llm",
                        lambda name, model, prompt, *a, **k:
                        panel.ReviewerRun(list(per_seat.get(name, [])), None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _adjudicate)
    out = tmp_path / f"r{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=2,
                     scope="auto") == 0
    return capsys.readouterr().out, json.loads(out.read_text())


def notes_about(payload, needle):
    return [n for n in payload["config_notes"] if needle in n]


# ------------------------------------------------------- it stands a solo finding down

def test_a_solo_finding_below_its_bands_threshold_is_not_the_fixers_work(
        monkeypatch, capsys, tmp_path):
    """The behaviour the dial is for. One seat raised a P3, the repo asks for two, and
    the finding leaves the fixer's list — which is the ONE list an orchestrator builds
    a brief from (`panel-review-pr.md` §5)."""
    report, payload = run(monkeypatch, capsys, tmp_path,
                          {"claude": [finding("P3")]},
                          config=cfg(threshold_by_severity={"P3": 2}))
    assert "### To fix (0)" in report
    assert "under the corroboration threshold" in report
    assert payload["to_fix"][0].get("below_threshold") is True
    assert payload["to_fix"][0].get("seats_required") == 2


def test_a_corroborated_finding_at_the_same_band_is_ordinary_work(
        monkeypatch, capsys, tmp_path):
    """The other side of the same threshold, and it is what says the dial counts SEATS
    rather than switching a band off. Two members raised the same defect, the band asks
    for two, and it is the fixer's work exactly as it was before #78."""
    report, payload = run(
        monkeypatch, capsys, tmp_path,
        {"claude": [finding("P3")], "codex": [finding("P3", seat="codex")]},
        config=cfg(seats=TWO_SEATS, threshold_by_severity={"P3": 2}))
    assert "### To fix (1)" in report
    assert "under the corroboration threshold" not in report
    assert payload["to_fix"][0].get("below_threshold") is False
    assert payload["to_fix"][0]["reviewers"] == ["claude", "codex"]


def test_one_seat_reporting_a_defect_twice_is_still_one_seat(
        monkeypatch, capsys, tmp_path):
    """`Canonical.reviewers` de-duplicates by member, and the threshold reads that
    rather than `reported_by`. A seat that files the same defect twice — a reply with a
    repeated block, a scanner run over two paths — would otherwise buy its own
    corroboration, which is the one way a count of accounts differs from a count of
    opinions."""
    _, payload = run(monkeypatch, capsys, tmp_path,
                     {"claude": [finding("P3"), finding("P3")]},
                     config=cfg(threshold_by_severity={"P3": 2}))
    assert payload["to_fix"][0].get("below_threshold") is True


# ---------------------------------------------------- what it may NEVER stand down

def test_a_threshold_may_never_stand_down_a_blocker(monkeypatch, capsys, tmp_path):
    """The catastrophic case, refused by the mechanism rather than by the default. A
    solo P1 that no other seat spotted is what the panel exists for; a head count
    cannot be trusted with the decision to ignore it, so the round applies 1 at both
    blocking bands whatever the repo wrote — and SAYS it did, because a policy the
    operator wrote and the round did not run is worse than a refused value."""
    report, payload = run(monkeypatch, capsys, tmp_path,
                          {"claude": [finding("P1"), finding("P2", title="leak")]},
                          config=cfg(threshold_by_severity={"P1": 2, "P2": 2}))
    assert "### To fix (2)" in report
    assert "under the corroboration threshold" not in report
    said = notes_about(payload, "will NOT apply")
    assert said and "P1, P2" in said[0]
    assert all(f["below_threshold"] is False for f in payload["to_fix"])


def test_a_p2_below_a_p1_trigger_floor_is_still_a_blocker(
        monkeypatch, capsys, tmp_path):
    """The case the trigger-floor bound alone gets WRONG, and the reason the second
    condition exists. At `round_trigger_floor: P1` a P2 is below the floor, so rules 1
    and 3 ignore it — but rule 2 blocks on `("P1", "P2")` whatever any floor says, so
    standing it down would leave a finding no fix pass may touch and a stop rule
    demanding it every round until the cap. `BLOCKING_SEVERITIES` is read from where
    rule 2 reads it, so the two cannot drift apart."""
    report, payload = run(monkeypatch, capsys, tmp_path,
                          {"claude": [finding("P2")]},
                          config=cfg(round_trigger_floor="P1",
                                     threshold_by_severity={"P2": 2}))
    assert "### To fix (1)" in report
    assert notes_about(payload, "will NOT apply")
    assert payload["to_fix"][0]["below_threshold"] is False


def test_a_sonar_gate_issue_is_never_stood_down_by_a_threshold(
        monkeypatch, capsys, tmp_path):
    """A hard-gate issue is a red quality gate, not a judged opinion, and it is filed
    by a scanner rather than by a panel — so "not enough seats agreed" is not a
    sentence that can be true of it. Same exemption both floors already have, at every
    rule."""
    issue = panel.Finding("sonarqube", "P3", "b.py", 7, "unused import",
                          "python:S1128")
    report, payload = run(monkeypatch, capsys, tmp_path, {"claude": []},
                          config=cfg(threshold_by_severity={"P3": 2, "P4": 2}),
                          sonar=[issue])
    assert "### SonarCloud issues (1)" in report
    assert payload["sonar_findings"][0].get("below_threshold") is False
    assert payload["sonar_findings"][0].get("seats_required") == 1


# -------------------------------------------------------------- it does not disappear

def test_a_stood_down_finding_keeps_its_verdict_its_row_and_its_seat_count(
        monkeypatch, capsys, tmp_path):
    """Reported, never suppressed. The finding is still master-confirmed, still in the
    payload, still on the board, and the report prints the count that stood it down
    beside it — so a human can disagree with the threshold on the evidence rather than
    on trust."""
    report, payload = run(monkeypatch, capsys, tmp_path,
                          {"claude": [finding("P3")]},
                          config=cfg(threshold_by_severity={"P3": 2}))
    assert payload["to_fix"][0]["verdict"] == "confirmed"
    assert payload["to_fix"][0]["reviewers"] == ["claude"]
    assert "1 of 2 seats" in report


def test_the_report_does_not_print_a_stood_down_finding_twice(
        monkeypatch, capsys, tmp_path):
    """A finding under the fix floor AS WELL is listed once, under the floor's heading,
    which already says it is not this round's work. Two headings for one defect would
    make a round's finding count read as double what it was — and the payload flags the
    two independently, so nothing about the disposal is lost by printing it once."""
    report, payload = run(monkeypatch, capsys, tmp_path,
                          {"claude": [finding("P3")]},
                          config=cfg(fix_severity_floor="P2",
                                     threshold_by_severity={"P3": 2}))
    assert report.count("[34-F01]") == 1
    assert "below the `P2` fix floor" in report
    assert payload["to_fix"][0]["below_fix_floor"] is True
    assert payload["to_fix"][0]["below_threshold"] is True


def test_a_stood_down_finding_is_not_offered_to_the_line_budget_either(
        monkeypatch, capsys, tmp_path):
    """`budgeted_fix` means "this round's work while the line budget lasts", and a
    stood-down finding is not this round's work in any sense. It is the one flag #78
    has to reach into that it did not add: a below-FLOOR finding is outside the budget
    by construction, and a below-THRESHOLD one sits squarely inside the budgeted band,
    so left alone the payload would offer a fixer budget to spend on a finding the
    report had taken out of its list."""
    _, payload = run(monkeypatch, capsys, tmp_path, {"claude": [finding("P3")]},
                     config=cfg(threshold_by_severity={"P3": 2}))
    assert payload["to_fix"][0]["below_threshold"] is True
    assert payload["to_fix"][0]["budgeted_fix"] is False


def test_the_default_never_warns_about_a_threshold_nobody_set(
        monkeypatch, capsys, tmp_path):
    """A round where no member filed has nothing to compare against, and every band's
    threshold is at least 1 — so a bare "threshold above the filer count" test would
    put the warning on every round of every repo that has never written the key. A
    warning about a policy nobody set is the alert fatigue this harness is careful
    about elsewhere, and it would take the real case down with it."""
    _, payload = run(monkeypatch, capsys, tmp_path, {"claude": []})
    assert not notes_about(payload, "more seats than filed")


def test_a_threshold_no_panel_this_size_can_reach_is_reported(
        monkeypatch, capsys, tmp_path):
    """A threshold above the number of members that filed anything stands down its
    WHOLE band, every round, for ever — and a report only ever shows what was stood
    down, so the config that did it is invisible from the artifact. Said where the two
    numbers are both known."""
    _, payload = run(monkeypatch, capsys, tmp_path, {"claude": [finding("P3")]},
                     config=cfg(threshold_by_severity={"P3": 3}))
    assert notes_about(payload, "more seats than filed")


def test_a_stood_down_finding_cannot_hold_the_cycle_open(
        monkeypatch, capsys, tmp_path):
    """The guarantee that let `round_stop` stay untouched. A band a threshold may act
    on is below the trigger floor and outside `BLOCKING_SEVERITIES`, so rule 1 does not
    buy a round for it when it is new, rule 2 does not block on it, and rule 3 does not
    buy one when it repeats. Asserted through the real stop rule rather than argued:
    the round that stands one down still stops."""
    _, payload = run(monkeypatch, capsys, tmp_path, {"claude": [finding("P3")]},
                     config=cfg(threshold_by_severity={"P3": 2}))
    assert payload["round_stop"]["stop"] is True


def test_the_shipped_default_leaves_the_round_exactly_as_it_was(
        monkeypatch, capsys, tmp_path):
    """`{}` is the off switch and is what ships, on `max_fix_guard_lines`' precedent:
    eight findings on two pull requests is an observation, not a calibration. A repo
    that has written nothing sees the sentence it saw before #78, no extra heading, and
    a threshold of 1 at every band."""
    report, payload = run(monkeypatch, capsys, tmp_path,
                          {"claude": [finding("P3")]})
    assert "master-confirmed, any reviewer count" in report
    assert "corroboration" not in report
    assert payload["review_panel"]["threshold_by_severity"] == {}
    assert payload["to_fix"][0]["below_threshold"] is False


# ------------------------------------------------------------------- the resolver

def test_the_documented_default_is_the_applied_default():
    """`harness_rules.DEFAULTS` is what an operator reads and `panel_core` is what the
    resolver falls back to. A drift is invisible from either side."""
    assert (harness_rules.DEFAULTS["review_panel"]["threshold_by_severity"]
            == panel_core.DEFAULT_THRESHOLD_BY_SEVERITY == {})


def test_a_band_is_read_the_way_every_other_severity_entering_the_panel_is():
    """Stripped and upper-cased, so a hand-edited rules file and a board dial spelling
    the same band differently resolve to one band rather than to two."""
    dials = panel_seats.resolve_dials({"threshold_by_severity": {" p3 ": "2"}},
                                      None, [])
    assert dials.threshold_by_severity == {"P3": 2}


def test_one_at_a_band_is_legal_and_is_the_identity():
    """Not a second spelling of the off switch: a repo that wants to say a band is
    deliberately left at one seat, beside a band that is not, can."""
    dials = panel_seats.resolve_dials(
        {"threshold_by_severity": {"P3": 1, "P4": 2}}, None, [])
    assert dials.threshold_for("P3") == 1 and dials.threshold_for("P4") == 2
    assert dials.thresholds_applied() is True


@pytest.mark.parametrize("value", [
    2,                                  # not a mapping at all
    {"P0": 2},                          # a band this panel does not have
    {"P3": 0},                          # no finding is raised by fewer than one seat
    {"P3": -1},
    {"P3": True},                       # `isinstance(True, int)` — the bool trap
    {"P3": 1.5},                        # half a seat is not a count
    {"P3": "lots"},
])
def test_a_malformed_threshold_is_a_hard_exit_naming_the_key(value):
    """A repo that typed a governance setting wrong and got default behaviour is the
    failure this block's hard exit exists to prevent: the review still runs, under a
    policy the file did not ask for, in the round the fixer is briefed from."""
    with pytest.raises(SystemExit) as e:
        panel_seats.resolve_dials({"threshold_by_severity": value}, None, [])
    assert "threshold_by_severity" in str(e.value)


def test_two_spellings_of_one_band_are_refused_rather_than_resolved():
    """Normalising on the way in is what makes `" p3 "` and `"P3"` one band — and it is
    also what lets both be written, after which one number silently wins on insertion
    order and a repo reading its own rules file cannot tell which. A hand that wrote
    two meant one of them, and nothing here can tell which."""
    with pytest.raises(SystemExit) as e:
        panel_seats.resolve_dials(
            {"threshold_by_severity": {"P3": 2, " p3 ": 3}}, None, [])
    assert "written twice" in str(e.value)
    assert "written twice" in harness_rules.dial_problem(
        "review_panel.threshold_by_severity", {"P3": 2, "p3": 3})


@pytest.mark.parametrize("unset", [None, ""])
def test_an_unset_threshold_takes_the_default_rather_than_switching_anything_off(unset):
    """There is no nullable switch to want. `{}` already spells "no threshold
    anywhere", so a written `null` meaning the same thing would be one value with two
    meanings — the collapse `unrefereed_line_weight` refuses one function up."""
    dials = panel_seats.resolve_dials({"threshold_by_severity": unset}, None, [])
    assert dials.threshold_by_severity == {}


def test_the_bound_reads_the_same_tuple_round_stop_rule_2_blocks_on():
    """One statement of which severities block, not two. A refactor that moved rule 2's
    bar and left this behind would be a threshold suppressing a finding rule 2 goes on
    demanding — the jam this bound exists to make unreachable."""
    assert panel_core.BLOCKING_SEVERITIES == ("P1", "P2")
    assert "BLOCKING_SEVERITIES" in Path(panel_rounds.__file__).read_text()
    dials = panel_seats.resolve_dials({"round_trigger_floor": "P1"}, None, [])
    assert not any(dials.corroboration_applies(b)
                   for b in panel_core.BLOCKING_SEVERITIES)


# ---------------------------------------------------------------------- the board dial

def test_the_dial_is_board_settable_and_its_value_is_checked_there_too():
    """`dial_problem` is the round's own judgement asked one step earlier. If the two
    could disagree, a value refused at the board would be one a round applied — or,
    worse, the other way round."""
    path = "review_panel.threshold_by_severity"
    assert path in harness_rules.BOARD_DIALS
    assert harness_rules.dial_problem(path, {"P3": 2}) == ""
    assert "whole number of seats" in harness_rules.dial_problem(path, {"P3": 0})
    assert "whole number of seats" in harness_rules.dial_problem(path, {"P3": True})
    assert "severity band" in harness_rules.dial_problem(path, {"nope": 2})
    assert "object keyed by severity band" in harness_rules.dial_problem(path, 2)


def test_the_dial_hint_carries_an_example_because_there_is_no_closed_set():
    """The one kind whose value is an object. A form cannot enumerate it, so offering
    nothing and hinting nothing would leave a person guessing at the shape."""
    hint = harness_rules.dial_hint("review_panel.threshold_by_severity")
    assert '{"P3": 2}' in hint and "null" not in hint
    assert harness_rules.dial_choices("review_panel.threshold_by_severity") == ()
