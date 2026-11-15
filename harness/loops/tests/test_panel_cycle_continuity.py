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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_classes  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
import panel_rounds  # noqa: E402
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


def test_a_baseline_without_round_continues_the_cycle_as_the_next_round(
        monkeypatch, capsys, tmp_path):
    """The nix-fleet run exactly: round 1, then `--baseline r1.json` with no
    `--round`. It is round 2 of round 1's cycle, and the baseline counts."""
    _, _, r1 = _cli(monkeypatch, capsys, tmp_path / "r1.json",
                    "--class-ledger", str(tmp_path / "classes.json"))
    _, _, r2 = _cli(monkeypatch, capsys, tmp_path / "r2.json",
                    "--baseline", str(tmp_path / "r1.json"))
    assert r2["round"] == 2
    assert r2["cycle"] == r1["cycle"]
    assert NOT_EARLIER not in _notes(r2)
    assert "--round not given: this is round 2, the round after --baseline's round 1" \
        in _notes(r2)


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


def test_a_ledger_with_earlier_rounds_and_no_baseline_says_a_new_cycle_starts(
        monkeypatch, capsys, tmp_path):
    """Second acceptance point: the run that cannot continue the cycle says so on
    stderr and in the payload's notes (which the posted summary prints)."""
    ledger_path = tmp_path / "classes.json"
    ledger = panel_classes.new_ledger("acme/board", 34)
    panel_classes.mark_round(ledger, 1)
    assert panel_classes.save_ledger(str(ledger_path), ledger) == 0
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json",
                     "--class-ledger", str(ledger_path))
    said = "the class ledger is at round 2 of this PR, but this panel run has no " \
           "--baseline, so it starts a NEW panel cycle"
    assert said in err
    assert said in _notes(r)


def test_a_later_round_with_no_cycle_to_continue_says_so(monkeypatch, capsys, tmp_path):
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--round", "2",
                     "--max-rounds", "3")
    said = "round 2 found no earlier round's cycle to continue (no --baseline was given)"
    assert said in err
    assert said in _notes(r)


def test_a_continued_round_says_nothing_about_a_lost_cycle(monkeypatch, capsys, tmp_path):
    _cli(monkeypatch, capsys, tmp_path / "r1.json",
         "--class-ledger", str(tmp_path / "classes.json"))
    _, err, r2 = _cli(monkeypatch, capsys, tmp_path / "r2.json",
                      "--class-ledger", str(tmp_path / "classes.json"),
                      "--baseline", str(tmp_path / "r1.json"))
    assert "NEW panel cycle" not in err
    assert "no earlier round's cycle" not in err + _notes(r2)


def _payload(tmp_path, name, **fields):
    path = tmp_path / name
    path.write_text(json.dumps({"github": "acme/board", "pr": 34, **fields}))
    return str(path)


@pytest.mark.parametrize("fields, expected", [
    ({"round": 2}, 2),
    ({"round": True}, None),
    ({"round": "2"}, None),
    ({"round": 0}, None),
    ({}, None),
])
def test_highest_baseline_round_reads_only_a_whole_round(tmp_path, fields, expected):
    assert panel_rounds.highest_baseline_round(
        [_payload(tmp_path, "b.json", **fields)], "acme/board", 34) == expected


def test_highest_baseline_round_ignores_another_reviews_and_unreadable_payloads(tmp_path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"github": "acme/other", "pr": 34, "round": 5}))
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    mine = _payload(tmp_path, "mine.json", round=2)
    got = panel_rounds.highest_baseline_round(
        [str(other), str(broken), str(tmp_path / "missing.json"), mine], "acme/board", 34)
    assert got == 2
