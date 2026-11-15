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


def _stub(monkeypatch, runs=()):
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
    monkeypatch.setattr(panel, "board_get",
                        lambda path, params: ({"runs": list(runs)}, ""))


def _cli(monkeypatch, capsys, out: Path, *extra, runs=()):
    """The run as the nix-fleet agent made it: through `main()`, flags only."""
    _stub(monkeypatch, runs)
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
    assert LOST not in err + _notes(r2)


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
MINTED = object()


def _assert_lost(err: str, r: dict, said: str, round_no: int, cycle) -> None:
    """The ONE way a test here checks a lost-cycle warning (#866): the message once
    on stderr and once in the notes, AND the round and cycle it describes, so a test
    cannot pin the text of a warning about a state it never looks at. `cycle` is
    None for "records cycle null", or MINTED for a new cycle this run started."""
    assert err.count(LOST) == 1 and _notes(r).count(LOST) == 1
    assert said in err and said in _notes(r)
    assert r["round"] == round_no
    if cycle is MINTED:
        assert r["cycle"] and r["cycle"] == r["run_key"], "a new cycle, minted here"
    else:
        assert r["cycle"] == cycle


def test_a_ledger_with_earlier_rounds_and_no_baseline_says_a_new_cycle_starts(
        monkeypatch, capsys, tmp_path):
    """Second acceptance point: the run that cannot continue the cycle says so on
    stderr and in the payload's notes (which the posted summary prints), and the
    state it describes is the state recorded."""
    ledger = _ledger_at(tmp_path / "classes.json", 1)
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--class-ledger", ledger)
    _assert_lost(err, r, "round 1 cannot continue this PR's cycle: no --baseline was "
                 "given, though the class ledger is at round 2 of this PR, so it starts "
                 "a NEW panel cycle", 1, MINTED)


def test_a_later_round_with_no_baseline_records_cycle_null_and_says_so(
        monkeypatch, capsys, tmp_path):
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--round", "2",
                     "--max-rounds", "3")
    _assert_lost(err, r, "round 2 cannot continue this PR's cycle: no --baseline was "
                 "given, so it records cycle null", 2, None)


def test_an_unusable_baseline_without_round_says_the_cycle_is_lost(
        monkeypatch, capsys, tmp_path):
    """A `--baseline` flag is not a baseline: another PR's payload is refused, the
    run is round 1, and it says it starts a new cycle instead of minting one quietly."""
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"github": "acme/board", "pr": 99, "round": 1,
                                 "cycle": "c-other"}))
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--baseline", str(other))
    _assert_lost(err, r, "round 1 cannot continue this PR's cycle: none of its "
                 "--baseline payloads was accepted (see the baseline notes), so it "
                 "starts a NEW panel cycle", 1, MINTED)


def test_a_non_utf8_baseline_is_reported_not_fatal(monkeypatch, capsys, tmp_path):
    """Beside a good round 1, so the round is attributable and every baseline reader
    runs over the bad file too, not only `load_baseline`."""
    _cli(monkeypatch, capsys, tmp_path / "r1.json", "--max-rounds", "4")
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe{")
    code, _, r = _cli(monkeypatch, capsys, tmp_path / "r.json",
                      "--baseline", str(tmp_path / "r1.json"), "--baseline", str(bad))
    assert code == 0
    assert r["round"] == 2
    assert "unreadable (UnicodeDecodeError)" in _notes(r)


@pytest.mark.parametrize("reader", ["earlier_round_files", "earlier_round_registers"])
def test_every_baseline_reader_skips_a_non_utf8_file(tmp_path, reader):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe{")
    getattr(panel, reader)([str(bad)], "acme/board", 34, 2, None)


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
    _assert_lost(err, r, "round 2 cannot continue this PR's cycle: its --baseline "
                 "payloads record no cycle id, so it records cycle null", 2, None)
    assert "see the baseline notes" not in err


def test_one_lost_cycle_is_one_warning(monkeypatch, capsys, tmp_path):
    ledger = _ledger_at(tmp_path / "classes.json", 1)
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", "--class-ledger", ledger,
                     "--round", "2", "--max-rounds", "3")
    _assert_lost(err, r, "round 2 cannot continue this PR's cycle: no --baseline was "
                 "given, though the class ledger is at round 2 of this PR, so it records "
                 "cycle null", 2, None)


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


@pytest.mark.parametrize("payload, expected", [
    ({}, 1), ({"round": None}, 1), ({"round": 0}, 1), ({"round": 2}, 2),
    ({"round": True}, None), ({"round": "2"}, None), ({"round": -1}, None),
])
def test_one_reading_of_a_payloads_round(payload, expected):
    assert panel_rounds.payload_round(payload) == expected


def test_a_negative_round_is_malformed_not_a_round_to_follow(monkeypatch, capsys,
                                                             tmp_path):
    base = {"github": "acme/board", "pr": 34, "round": -5, "cycle": "c-1"}
    (tmp_path / "b.json").write_text(json.dumps(base))
    _, _, r = _cli(monkeypatch, capsys, tmp_path / "r.json",
                   "--baseline", str(tmp_path / "b.json"))
    assert r["round"] == 2
    assert "has a malformed round (-5) — counted as round 1" in _notes(r)


def test_a_missing_round_reads_the_same_in_every_baseline_reader(tmp_path):
    """A payload with no `round` is round 1 to `load_baseline`, so the readers beside
    it must not skip it as unreadable."""
    base = {"github": "acme/board", "pr": 34, "cycle": "c-1",
            "coverage_declarations": [{"key": "ca-1", "declaration": "x"}]}
    (tmp_path / "b.json").write_text(json.dumps(base))
    declared = panel.earlier_round_registers([str(tmp_path / "b.json")], "acme/board",
                                             34, 2, "c-1")
    assert "ca-1" in json.dumps(declared, default=str)


def test_continue_cycle_needs_only_the_baseline(monkeypatch, capsys, tmp_path):
    """The relaxed refusal: `--continue-cycle --baseline r1.json` with no `--round`
    continues the stopped cycle as round 2."""
    _, _, r1 = _cli(monkeypatch, capsys, tmp_path / "r1.json", "--max-rounds", "4")
    stopped = {"id": "run-1", "round": 1, "cycle": r1["cycle"], "head_sha": HEAD,
               "ts": "2026-11-15T05:00:00+00:00", "stopped": True,
               "stop_reason": "84% of new findings were introduced by the fix pass",
               "stop_confident": False, "stop_veto": []}
    _, _, r2 = _cli(monkeypatch, capsys, tmp_path / "r2.json",
                    "--baseline", str(tmp_path / "r1.json"), "--continue-cycle",
                    "--answered-by", "owner", "--why", "run round 2", runs=[stopped])
    assert r2["round"] == 2 and r2["cycle"] == r1["cycle"]
    assert r2["stop_answered"]["by"] == "owner"


def test_every_lost_cycle_warning_here_is_checked_through_assert_lost():
    """The guard on `_assert_lost`: a test that names the warning's text without
    calling it would be the text-only check the helper exists to rule out."""
    import ast
    tree = ast.parse(Path(__file__).read_text())
    for fn in tree.body:
        if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("test_")):
            continue
        texts = [n.value for n in ast.walk(fn)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        calls = {n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if any(LOST in t for t in texts):
            assert "_assert_lost" in calls, fn.name


def test_no_message_tells_a_caller_round_is_required_beside_a_baseline():
    """#866 made `--round` optional whenever a `--baseline` is accepted, and every
    remedy that still demanded it was stale text in front of a caller."""
    src = (Path(__file__).resolve().parent.parent / "panel.py").read_text()
    for stale in ("with --round", "--round and --baseline alone",
                  "(default 1). Round 2+"):
        assert stale not in src, stale


@pytest.mark.parametrize("round_flag", [[], ["--round", "2"]],
                         ids=["inferred", "explicit"])
def test_every_run_reads_its_baseline_once_with_one_identity(
        monkeypatch, capsys, tmp_path, round_flag):
    """#868: the inferred-round run and the explicit-round run read the baseline ONCE
    each, naming the review the same way, so the two cannot drift apart."""
    _cli(monkeypatch, capsys, tmp_path / "r1.json", "--max-rounds", "4")
    seen = []
    real = panel.load_baseline

    def spy(paths, expect=None):
        seen.append({k: v for k, v in (expect or {}).items() if k != "round"})
        return real(paths, expect)

    monkeypatch.setattr(panel, "load_baseline", spy)
    _, _, r2 = _cli(monkeypatch, capsys, tmp_path / "r2.json", *round_flag,
                    "--baseline", str(tmp_path / "r1.json"))
    assert r2["round"] == 2
    assert seen == [{"repo": "board", "github": "acme/board", "pr": 34}]


@pytest.mark.parametrize("extra", [[], ["refused"]], ids=["all-accepted", "one-refused"])
def test_accepted_baselines_with_no_cycle_id_say_their_findings_still_count(
        monkeypatch, capsys, tmp_path, extra):
    """#868: payloads that were accepted but record no cycle id still carry their
    findings, so the warning must not say nothing carries, nor send the caller to
    re-pass them or drop them for --new-cycle, neither of which supplies the id."""
    base = {"github": "acme/board", "pr": 34, "round": 1, "reviewed": True}
    (tmp_path / "r1.json").write_text(json.dumps(base))
    args = ["--baseline", str(tmp_path / "r1.json")]
    if extra:
        (tmp_path / "other.json").write_text(json.dumps({**base, "pr": 99}))
        args += ["--baseline", str(tmp_path / "other.json")]
    _, err, r = _cli(monkeypatch, capsys, tmp_path / "r.json", *args)
    _assert_lost(err, r, "its --baseline payloads record no cycle id, so it records "
                 "cycle null", 2, None)
    assert r["prior_rounds"] == 1
    assert "Their findings still count as earlier rounds" in err
    assert "No earlier round's findings" not in err
    assert "pass every earlier round's payload" not in err
    assert "--new-cycle" not in err
