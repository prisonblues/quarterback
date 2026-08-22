"""`qb-pace` — the verdict a caller can act on, and the exit status that carries it.

`qbdata.pace()` decides; this file is about the half that makes the decision
reachable from a shell script and from `qb-seat`. Three properties, in the order
they matter:

  1. **A ceiling that could not be read is never reported as clear.** `--gate`
     answers 4 for that and 3 for a spent window, and they are different codes on
     purpose: a caller may reasonably decide to run on an unreadable ceiling and
     may not reasonably decide to run on a spent one (#244).
  2. **Being told is the default and being stopped is opt-in.** Plain `qb-pace`
     exits 0 whatever the window is doing.
  3. **The estimate refuses to predict the fit.** It reports what the job costs
     and what is left, and says in as many words that nothing records the rate
     between them — a made-up number would arrive beside two real ones.

Run: pytest harness/tests/test_qb_pace.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))


def _load():
    """`qb-pace` has no `.py`, so it is loaded by path, as test_qb_reconcile.py does."""
    loader = importlib.machinery.SourceFileLoader("qb_pace", str(BIN / "qb-pace"))
    spec = importlib.util.spec_from_loader("qb_pace", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qb_pace"] = module
    loader.exec_module(module)
    return module


qp = _load()


@pytest.fixture(autouse=True)
def _no_endpoint(monkeypatch):
    """Nothing here may reach the usage endpoint or the board.

    Both are stubbed by default and re-stubbed per test. A suite that read the
    developer's real subscription would pass or fail on what they happened to have
    spent that afternoon, and one that reached the network at all is its own
    defect — this repo says so in the panel's own rules.
    """
    monkeypatch.setattr(qp, "pace", lambda: _verdict("go"))
    monkeypatch.setattr(qp, "subscription_cost",
                        lambda *a, **k: (None, "stubbed: the board was not asked"))


def _verdict(kind: str, **over) -> dict:
    base = {"go": {"verdict": "go", "source": "live", "reason": "5h at 12%",
                   "cap": "5h", "percent": 12, "severity": "normal", "resets_in_s": 9000},
            "slow": {"verdict": "slow", "source": "live", "reason": "5h at 74%",
                     "cap": "5h", "percent": 74, "severity": "normal", "resets_in_s": 2850},
            "hold": {"verdict": "hold", "source": "live", "reason": "5h at 96%",
                     "cap": "5h", "percent": 96, "severity": "normal", "resets_in_s": 2850},
            "unknown": {"verdict": "unknown", "source": "unreadable",
                        "reason": "the usage endpoint could not be read (HTTP 500)",
                        "cap": None, "percent": None, "severity": None,
                        "resets_in_s": None}}[kind]
    return {**base, **over}


def _at(monkeypatch, kind: str) -> None:
    monkeypatch.setattr(qp, "pace", lambda: _verdict(kind))


def test_without_a_gate_it_always_exits_zero_and_always_says_something(monkeypatch, capsys):
    """Being told is the whole of the gap this closes. A command that started
    failing in scripts because a window was warm would be the larger claim."""
    for kind in ("go", "slow", "hold", "unknown"):
        _at(monkeypatch, kind)
        assert qp.main([]) == 0
        assert kind.upper() in capsys.readouterr().out


def test_a_gate_answers_three_for_a_spent_window_and_four_for_an_unreadable_one(
        monkeypatch, capsys):
    """RED/GREEN, and the more important half is the four. A governor that cannot
    read its input must not report clear (#244) — but a dropped network is not a
    spent window either, so a caller that is willing to run on one and not on the
    other can tell them apart without parsing anything."""
    _at(monkeypatch, "hold")
    assert qp.main(["--gate"]) == 3
    assert "HOLD" in capsys.readouterr().out
    _at(monkeypatch, "unknown")
    assert qp.main(["--gate"]) == 4
    assert "UNKNOWN" in capsys.readouterr().out


def test_a_gate_does_not_stop_work_merely_because_the_window_is_warm(monkeypatch, capsys):
    """`slow` means spend less, not stop, and what to turn down is #276's business.
    It still says so — the caller relays a line it did not have to parse."""
    _at(monkeypatch, "slow")
    assert qp.main(["--gate"]) == 0
    assert "SLOW" in capsys.readouterr().out


def test_a_gate_says_nothing_at_all_when_there_is_nothing_to_act_on(monkeypatch, capsys):
    """A line printed on every seat start is how an operator learns to skip the one
    that means something — qb-seat's own rule, and the reason it can relay this
    output verbatim without deciding what to suppress."""
    _at(monkeypatch, "go")
    assert qp.main(["--gate"]) == 0
    assert capsys.readouterr().out == ""


def test_the_json_shape_is_the_same_document_whatever_the_verdict(monkeypatch, capsys):
    """A parser wants one shape every time, so `--gate`'s silence does not apply to
    it: its "nothing to say" is a field, not an empty stream."""
    _at(monkeypatch, "go")
    assert qp.main(["--json", "--gate"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["verdict"] == "go" and got["cap"] == "5h"


def test_the_estimate_reports_the_job_and_the_headroom_and_refuses_the_fit(
        monkeypatch, capsys):
    """The fit line is the point. Nothing records how much of a window a seat-run
    spends, so it says that rather than multiplying two real numbers by a made-up
    third one."""
    _at(monkeypatch, "slow")
    monkeypatch.setattr(qp, "subscription_cost",
                        lambda *a, **k: ({"tokens_per_run": 283_795, "runs": 45,
                                          "models": ["opus"]}, None))
    assert qp.main(["--estimate", "5", "--rounds", "2"]) == 0
    out = capsys.readouterr().out
    assert "2,837,950 tokens" in out
    assert "26% of 5h left" in out
    assert "resets in 47m" in out
    assert "fit       unknown" in out


def test_an_estimate_the_board_cannot_price_says_so_and_still_reports_the_headroom(
        monkeypatch, capsys):
    _at(monkeypatch, "slow")
    monkeypatch.setattr(qp, "subscription_cost",
                        lambda *a, **k: (None, "the board did not answer (OSError)"))
    assert qp.main(["--estimate", "4"]) == 0
    out = capsys.readouterr().out
    assert "estimate  unknown — the board did not answer (OSError)" in out
    assert "26% of 5h left" in out


def test_an_estimate_rides_inside_the_json_rather_than_beside_it(monkeypatch, capsys):
    _at(monkeypatch, "go")
    monkeypatch.setattr(qp, "subscription_cost",
                        lambda *a, **k: ({"tokens_per_run": 100, "runs": 2,
                                          "models": ["opus"]}, None))
    assert qp.main(["--json", "--estimate", "3"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["estimate"]["tokens"] == 300
    assert got["estimate"]["fits"] is None


def test_a_seat_count_below_one_is_a_usage_error(monkeypatch):
    _at(monkeypatch, "go")
    with pytest.raises(SystemExit) as exc:
        qp.main(["--estimate", "0"])
    assert exc.value.code == 2
