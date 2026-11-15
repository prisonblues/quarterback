"""#623 slice 2: what the judge typed decides what blocks.

A demonstrated ``regression`` or ``claim_miss`` blocks at any severity, an
``observation`` blocks nothing (no round, no strike, no issue), and an untyped
finding keeps the severity floors it had before."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_classes as pc  # noqa: E402
import test_panel_dials as dials  # noqa: E402


def _finding(severity, kind="", title="boom", file="a.py"):
    reported = [panel.Finding("claude", severity, file, 1, title, "")]
    evidence = "test_x fails" if kind in ("regression", "claim_miss") else ""
    return panel.Canonical(id="34-F01", severity=severity, file=file, line=1,
                           synthesis=title, verdict="confirmed", reported_by=reported,
                           kind=kind, kind_said=kind, evidence=evidence)


def _stop(c, **floors):
    return panel.round_stop(2, 5, [c.key], [c], [], **floors)


def test_a_p1_observation_alone_stops_the_cycle_converged():
    c = _finding("P1", "observation")
    d = _stop(c)
    assert d["stop"] is True and d["confident"] is True
    assert d["converged"] is True
    assert d["observed"] == [c.key]
    assert d["outstanding"]["fixable"] == []


def test_a_p4_regression_buys_a_round_under_a_p2_trigger_floor():
    c = _finding("P4", "regression")
    d = _stop(c, trigger_floor="P2", cleared_floor="P2")
    assert d["stop"] is False
    assert d["outstanding"]["fixable"] == [c.key]


def test_an_untyped_p1_still_blocks_as_before():
    c = _finding("P1")
    d = _stop(c)
    assert d["stop"] is False
    assert d["observed"] == []


def _ledger(*kinds):
    ledger = pc.new_ledger("acme/board", 34)
    for round_no, kind in enumerate(kinds, start=1):
        pc.mark_round(ledger, round_no)
        pc.record(ledger, "resolver drift", round_no, f"a.py:{round_no}",
                  invariant="one resolver", kind=kind)
    return ledger, pc.find_class(ledger, "resolver drift")


def test_observation_sightings_are_no_strike_and_owe_nothing():
    _, entry = _ledger("observation", "observation", "observation")
    assert pc.strikes(entry) == 0
    assert pc.is_owed(entry, 2) is False


def test_a_class_with_only_observations_is_refused_before_gh_is_called(monkeypatch):
    def gh(*a):
        raise AssertionError("gh must not be called for an observation")
    monkeypatch.setattr(pc, "gh", gh)
    ledger, _ = _ledger("observation")
    with pytest.raises(ValueError, match="not a demonstrated failure"):
        pc.file_class(ledger, "resolver drift", 1, "pre-existing", "acme/board", 34)


def test_record_reads_the_kind_from_the_payload(tmp_path):
    payload = tmp_path / "r1.json"
    payload.write_text('{"to_fix": [{"id": "34-F02", "key": "k2", "kind": "regression"}]}')
    assert pc.payload_kind(str(payload), "34-F02") == "regression"
    assert pc.payload_kind(str(payload), "k2") == "regression"
    assert pc.payload_kind(str(payload), "34-F09") == ""


def test_an_observation_is_listed_apart_from_to_fix(monkeypatch, tmp_path, capsys):
    def adjudicate(clusters, *a, **k):
        flat = [f for grp in clusters for f in grp]
        kinds = {"P2": ("regression", "test_y fails"), "P1": ("observation", "")}
        return ([panel.Canonical(id=panel._finding_id(34, i + 1), severity=f.severity,
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", reported_by=[f], rationale="real",
                                 kind=kinds[f.severity][0], kind_said=kinds[f.severity][0],
                                 evidence=kinds[f.severity][1])
                 for i, f in enumerate(flat)], None, panel.CoverageRuling())

    dials.stub(monkeypatch, [dials.finding("P2", "breaks y"),
                             dials.finding("P1", "could be general", file="b.py")])
    monkeypatch.setattr(panel, "adjudicate", adjudicate)
    out = tmp_path / "r1.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=1, baseline=[], max_rounds=2, scope="auto") == 0
    report = capsys.readouterr().out
    assert "### To fix (1)" in report
    assert "### Observations, not blocking (1)" in report
    assert "could be general" in report.split("### Observations, not blocking")[1]


def test_a_later_record_of_the_same_sighting_fills_its_kind():
    """879-F11: the dedup kept the first record whole, so a sighting first recorded
    untyped could never become fileable, which is the route `file`'s error names."""
    ledger = pc.new_ledger("acme/board", 34)
    pc.mark_round(ledger, 1)
    pc.record(ledger, "resolver drift", 1, "a.py:1", invariant="one resolver")
    pc.record(ledger, "resolver drift", 1, "a.py:1", kind="regression")
    [sighting] = pc.find_class(ledger, "resolver drift")["sightings"]
    assert sighting["kind"] == "regression"


def test_a_p4_claim_miss_buys_a_round_too():
    c = _finding("P4", "claim_miss")
    assert _stop(c, trigger_floor="P2", cleared_floor="P2")["stop"] is False


def test_a_dry_stop_says_how_many_observations_it_held():
    d = panel.round_stop(1, 5, [], [_finding("P2", "observation")], [])
    assert d["stop"] is True and "1 observation(s), none blocking" in d["reason"]


def test_an_observation_sharing_a_key_with_a_regression_does_not_hide_it():
    """879 round 2, F01: keys are file + title, so two findings can share one, and
    the regression's must survive the subtraction."""
    reg, obs = _finding("P4", "regression"), _finding("P4", "observation")
    assert reg.key == obs.key
    d = panel.round_stop(2, 5, [reg.key], [reg, obs], [],
                         trigger_floor="P2", cleared_floor="P2")
    assert d["stop"] is False and d["observed"] == []
