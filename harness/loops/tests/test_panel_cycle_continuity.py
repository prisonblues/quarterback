"""#866: a re-review continues its PR's cycle, or says loudly that it does not.

On prisonblues/nix-fleet#162 three rounds ran with `--class-ledger` and `--baseline`
but no `--round`. Round 1 recorded `cycle: null` (a ledger round was not a cycle
round), and rounds 2 and 3 each defaulted to round 1, so `load_baseline` refused
their baseline as "not earlier than this run's round 1" and each started a new
cycle. fix_injection and the cycle's provenance never applied across the rounds,
and nothing but a config note said so.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_classes  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/nonexistent/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}

HEAD = "abc1230000000000000000000000000000000000"
NOT_EARLIER = "which is not earlier than this run's round"


def _stub(monkeypatch):
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "feat/x", "headRefOid": HEAD},
        diff="diff --git a/a.py b/a.py\n+x\n",
        compare='{"status": "ahead", "files": [{"filename": "a.py", "patch": "@@"}]}'))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    monkeypatch.setattr(panel, "board_get", lambda path, params: ({"runs": []}, ""))


def _cli(monkeypatch, capsys, out: Path, *extra):
    """The run as the nix-fleet agent made it: through `main()`, flags only."""
    _stub(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["panel.py", "--repo", "board", "--pr", "34",
                                      "--no-record", "--json-file", str(out), *extra])
    code = panel.main()
    captured = capsys.readouterr()
    return code, captured.err, json.loads(out.read_text())


def _notes(payload) -> str:
    return "\n".join(payload.get("config_notes") or [])


def test_a_class_ledger_round_1_mints_a_cycle(monkeypatch, capsys, tmp_path):
    """The first link: a ledger round that recorded `cycle: null` left round 2
    nothing to continue."""
    _, _, r1 = _cli(monkeypatch, capsys, tmp_path / "r1.json",
                    "--class-ledger", str(tmp_path / "classes.json"))
    assert r1["round"] == 1
    assert r1["cycle"], "a class-ledger round is a cycle round and must name its cycle"


def _ledger_at(path: Path, *rounds: int) -> str:
    ledger = panel_classes.new_ledger("acme/board", 34)
    for r in rounds:
        panel_classes.mark_round(ledger, r)
    assert panel_classes.save_ledger(str(path), ledger) == 0
    return str(path)


def test_the_nix_fleet_sequence_continues_the_cycle_as_the_next_round(
        monkeypatch, capsys, tmp_path):
    """nix-fleet#162 exactly: `--class-ledger` on every round, `mark` between them,
    and round 2 given `--baseline r1.json` but no `--round`. It is round 2 of round
    1's cycle, the baseline counts, and nothing warns."""
    ledger = str(tmp_path / "classes.json")
    _, _, r1 = _cli(monkeypatch, capsys, tmp_path / "r1.json", "--class-ledger", ledger)
    _ledger_at(Path(ledger), 1)
    _, err, r2 = _cli(monkeypatch, capsys, tmp_path / "r2.json", "--class-ledger", ledger,
                      "--baseline", str(tmp_path / "r1.json"))
    assert r2["round"] == 2
    assert r2["cycle"] == r1["cycle"]
    assert r2["class_ledger"]["round"] == 2
    assert NOT_EARLIER not in _notes(r2)
    assert "--round not given: this is round 2, the round after --baseline's round 1" \
        in _notes(r2)
    assert "cannot continue this PR's cycle" not in err + _notes(r2)


def test_the_round_follows_the_LATEST_baseline(monkeypatch, capsys, tmp_path):
    _, _, r1 = _cli(monkeypatch, capsys, tmp_path / "r1.json", "--max-rounds", "4")
    _cli(monkeypatch, capsys, tmp_path / "r2.json", "--baseline", str(tmp_path / "r1.json"))
    _, _, r3 = _cli(monkeypatch, capsys, tmp_path / "r3.json",
                    "--baseline", str(tmp_path / "r1.json"),
                    "--baseline", str(tmp_path / "r2.json"))
    assert r3["round"] == 3
    assert r3["cycle"] == r1["cycle"]


def test_an_explicit_round_still_wins(monkeypatch, capsys, tmp_path):
    _cli(monkeypatch, capsys, tmp_path / "r1.json", "--max-rounds", "4")
    _, _, r3 = _cli(monkeypatch, capsys, tmp_path / "r3.json", "--round", "3",
                    "--baseline", str(tmp_path / "r1.json"))
    assert r3["round"] == 3
    assert "--round not given" not in _notes(r3)


LOST = "cannot continue this PR's cycle"


def test_a_ledger_with_earlier_rounds_and_no_baseline_says_a_new_cycle_starts(
        monkeypatch, capsys, tmp_path):
    """Second acceptance point: the run that cannot continue the cycle says so on
    stderr and in the payload's notes (which the posted summary prints), and the
    state it describes is the state recorded."""
    ledger = _ledger_at(tmp_path / "classes.json", 1)
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--class-ledger", ledger)
    said = ("round 1 cannot continue this PR's cycle: no --baseline was given, though "
            "the class ledger is at round 2 of this PR, so it starts a NEW panel cycle")
    assert said in err
    assert said in _notes(r)
    assert r["round"] == 1 and r["cycle"], "a new cycle, minted here"


def test_a_later_round_with_no_baseline_records_cycle_null_and_says_so(
        monkeypatch, capsys, tmp_path):
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--round", "2",
                     "--max-rounds", "3")
    said = ("round 2 cannot continue this PR's cycle: no --baseline was given, so it "
            "records cycle null")
    assert said in err
    assert said in _notes(r)
    assert r["cycle"] is None


def test_an_unusable_baseline_without_round_says_the_cycle_is_lost(
        monkeypatch, capsys, tmp_path):
    """A `--baseline` flag is not a baseline: another PR's payload is refused, the
    run is round 1, and it says it starts a new cycle instead of minting one quietly."""
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"github": "acme/board", "pr": 99, "round": 1,
                                 "cycle": "c-other"}))
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--baseline", str(other))
    said = ("round 1 cannot continue this PR's cycle: none of its --baseline payloads "
            "was accepted (see the baseline notes), so it starts a NEW panel cycle")
    assert said in err
    assert r["round"] == 1 and r["cycle"] != "c-other"


def test_a_non_utf8_baseline_is_reported_not_fatal(monkeypatch, capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe{")
    code, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--baseline", str(bad))
    assert code == 0
    assert "unreadable (UnicodeDecodeError)" in _notes(r)
    assert "none of its --baseline payloads was accepted" in err


def test_a_baseline_with_no_round_field_is_round_1_for_inference_too(
        monkeypatch, capsys, tmp_path):
    """`load_baseline` reads a payload with no `round` as round 1, and the inferred
    round is read off what it accepted, so the two cannot disagree."""
    _cli(monkeypatch, capsys, tmp_path / "r1.json", "--max-rounds", "4")
    r1 = json.loads((tmp_path / "r1.json").read_text())
    del r1["round"]
    (tmp_path / "r1.json").write_text(json.dumps(r1))
    _, _, r2 = _cli(monkeypatch, capsys, tmp_path / "r2.json",
                    "--baseline", str(tmp_path / "r1.json"))
    assert r2["round"] == 2
    assert r2["cycle"] == r1["cycle"]
    assert NOT_EARLIER not in _notes(r2)


def test_a_concurrent_cycles_rounds_do_not_set_the_round(monkeypatch, capsys, tmp_path):
    """Cycle A's round 1 names the cycle, so cycle B's round 3 is refused and does
    not make this round 4 of A."""
    _cli(monkeypatch, capsys, tmp_path / "a1.json", "--max-rounds", "6")
    b3 = json.loads((tmp_path / "a1.json").read_text())
    b3.update(round=3, cycle="c-b", run_key="c-b")
    (tmp_path / "b3.json").write_text(json.dumps(b3))
    a1 = json.loads((tmp_path / "a1.json").read_text())
    _, _, r = _cli(monkeypatch, capsys, tmp_path / "r.json",
                   "--baseline", str(tmp_path / "a1.json"),
                   "--baseline", str(tmp_path / "b3.json"))
    assert r["round"] == 2
    assert r["cycle"] == a1["cycle"]


def test_an_accepted_baseline_with_no_cycle_id_is_named_as_such(
        monkeypatch, capsys, tmp_path):
    base = {"github": "acme/board", "pr": 34, "round": 1, "reviewed": True}
    (tmp_path / "r1.json").write_text(json.dumps(base))
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json",
                     "--baseline", str(tmp_path / "r1.json"))
    assert r["round"] == 2
    assert "its --baseline payloads record no cycle id" in err
    assert "see the baseline notes" not in err


def test_one_lost_cycle_is_one_warning(monkeypatch, capsys, tmp_path):
    ledger = _ledger_at(tmp_path / "classes.json", 1)
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--class-ledger", ledger,
                     "--round", "2", "--max-rounds", "3")
    assert err.count(LOST) == 1
    assert _notes(r).count(LOST) == 1
    assert "records cycle null" in err


def test_a_round_inferred_past_the_cap_names_the_baseline_not_a_flag(
        monkeypatch, capsys, tmp_path):
    base = {"github": "acme/board", "pr": 34, "round": 3, "cycle": "c-1"}
    (tmp_path / "r3.json").write_text(json.dumps(base))
    try:
        _cli(monkeypatch, capsys, tmp_path / "r.json", "--max-rounds", "3",
             "--baseline", str(tmp_path / "r3.json"))
    except SystemExit as stop:
        said = str(stop)
    else:
        raise AssertionError("round 4 of a 3-round cap ran")
    assert "--baseline (its latest round is 3), so round 4 is past the cap of 3" in said
