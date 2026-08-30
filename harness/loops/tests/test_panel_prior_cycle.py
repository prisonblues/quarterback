"""#617: the cycle has to remember that it was told to stop.

Rounds 3, 4 and 5 of `prisonblues/lexray#1780` were standalone `/panel` invocations
after round 2 had already ended the cycle on `escalate_on.fix_injection` at 84%
introduced. Nothing failed. Three new workflows simply started with no memory that a
terminal verdict existed, because each invocation is round 1 of its OWN cycle and
every convergence guard — `fix_injection`, `premise_repeated`, `max_fix_growth`, the
trend table — is keyed on a baseline the new workflow does not have. The cycle went
5 → 7 → 12 findings with two P1s in fix-pass-authored lines while nothing objected,
and the record was on the board the whole time.

So the round asks the board first, and a PR whose last recorded round ended the cycle
is REFUSED. `--force` does not move it, and deliberately: that flag says "this diff is
worth reading anyway", which is not an answer to "an earlier round already ended
this". `--new-cycle` is, and it says so in its own name — the opt-in is recorded
loudly rather than merely honoured, because "the tool chose to run" and "a caller
overrode the tool" must never look alike.

BEST-EFFORT, and that is a constraint on the feature rather than a caveat on it: a
board that is down or unconfigured must not stop a review, so a failed lookup says it
could not be checked rather than that there was nothing to find.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/nonexistent/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}

HEAD = "abc1230000000000000000000000000000000000"


def _run_row(round_no, *, stopped=True, reason="dry — nothing raised",
             confident=True, cycle="cyc-1111", head=HEAD,
             ts="2026-08-29T10:00:00+00:00"):
    """One row of `GET /review/findings`' `runs[]`, in the four fields #617 reads."""
    return {"id": f"run-{round_no}", "round": round_no, "cycle": cycle,
            "head_sha": head, "ts": ts, "stopped": stopped,
            "stop_reason": reason, "stop_confident": confident, "stop_veto": []}


def _board(body, err=""):
    """A `board_get` double. It also records what was asked, because the endpoint's
    own contract is that the summary and the per-run rows are read differently."""
    def get(path, params):
        return body, err
    return get


# ------------------------------------------------------- reading the verdict

def test_the_verdict_is_read_off_the_per_run_rows():
    """Never off the top-level summary: `stopped`/`stop_reason` there are NULLED
    whenever the traced window holds more than one cycle (#44) — which is precisely
    the shape a PR takes once somebody has run a second loop on it, so a reader that
    took the summary would go blind on exactly the pull requests this exists for."""
    got, why = panel.board_terminal_verdict("acme/board", 1780, get=_board({
        "stopped": None, "stop_reason": None,
        "runs": [_run_row(1, stopped=False, reason=None),
                 _run_row(2, reason="84% of new findings were introduced by the fix "
                                    "pass", confident=False)]}))
    assert why == ""
    assert got["round"] == 2 and got["cycle"] == "cyc-1111"
    assert got["confident"] is False
    assert "introduced by the fix pass" in got["reason"]


def test_the_NEWEST_stopped_round_wins():
    """A PR can hold several ended cycles, and the question is whether the last thing
    anybody recorded was a stop — not whether a stop ever happened."""
    got, _why = panel.board_terminal_verdict("acme/board", 1780, get=_board({
        "runs": [_run_row(2, cycle="old", reason="the first cycle stopped"),
                 _run_row(1, cycle="new", reason="and so did the second")]}))
    assert got["cycle"] == "new" and got["reason"] == "and so did the second"


def test_a_PR_whose_rounds_all_went_again_has_no_terminal_verdict():
    """The ordinary case, and it must be silent: a cycle mid-flight is not a cycle
    that ended, and reporting one would refuse every round after the first."""
    assert panel.board_terminal_verdict("acme/board", 1780, get=_board({
        "runs": [_run_row(1, stopped=False),
                 _run_row(2, stopped=False)]})) == (None, "")


@pytest.mark.parametrize("body,err,expected", [
    (None, "connection refused", "could not be asked"),
    ("a string, not an object", "", "not an object"),
    ({"stopped": False}, "", "published no `runs`"),
])
def test_a_lookup_that_could_not_be_made_is_reported_rather_than_read_as_nothing(
        body, err, expected):
    """"No terminal verdict" and "we could not find out" have different remedies, and
    only the first of them is a PR a fresh cycle may start on without anybody being
    told. The third row is a board too old to publish `runs[]` at all — named as a
    capability answer rather than as "no cycle ended", because an absence with two
    causes must not be read as the benign one."""
    got, why = panel.board_terminal_verdict("acme/board", 1780, get=_board(body, err))
    assert got is None and expected in why


# --------------------------------------------------------- what a round does with it

def _round(monkeypatch, capsys, tmp_path, *, runs=(), err="", force=False,
           new_cycle=False, head=HEAD):
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "feat/x", "headRefOid": head},
        diff="diff --git a/a.py b/a.py\n+x\n",
        compare='{"status": "ahead", "files": [{"filename": "a.py", "patch": "@@"}]}'))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    monkeypatch.setattr(panel, "board_get",
                        _board({"runs": list(runs)} if not err else None, err))
    out = tmp_path / "r.json"
    code = panel.run("board", 34, post=False, json_file=str(out), record=False,
                     force=force, new_cycle=new_cycle)
    return code, capsys.readouterr().out, json.loads(out.read_text())


STOPPED = (_run_row(2, reason="84% of new findings were introduced by the fix pass "
                              "before this round", confident=False),)


def test_a_round_on_an_ENDED_cycle_is_refused(monkeypatch, capsys, tmp_path):
    """The refusal itself. Exit 0 and a payload either way — a caller that treated a
    refusal as a crash would retry it, and a consumer given no payload reads the empty
    stdout as a clean PR, which is this repo's standing disease."""
    code, report, payload = _round(monkeypatch, capsys, tmp_path, runs=STOPPED)
    assert code == 0
    assert payload["reviewed"] is False
    assert payload["preflight"]["verdict"] == "refuse"
    assert "already ENDED this cycle" in payload["preflight"]["reason"]
    assert "already ENDED this cycle" in payload["skip_reason"]


def test_the_refusal_carries_the_record_it_was_made_from(monkeypatch, capsys,
                                                         tmp_path):
    """A reader auditing a refusal is owed the verdict it was made from, in the
    payload rather than only in the sentence — which is what `preflight.prior_cycle`
    is for, and it is why the record is carried through the gate rather than
    re-derived by whoever wants it."""
    _code, _report, payload = _round(monkeypatch, capsys, tmp_path, runs=STOPPED)
    pc = payload["preflight"]["prior_cycle"]
    assert pc["round"] == 2 and pc["confident"] is False
    assert pc["refused"] is True
    assert "introduced by the fix pass" in pc["reason"]


def test_the_notice_names_the_remedies_that_exist_and_the_one_that_does_not(
        monkeypatch, capsys, tmp_path):
    """The ordinary refusal's remedies are all about a branch that cannot merge, and
    every one of them is the wrong instruction here: rebasing does not undo a stop and
    no ceiling was consulted. A refusal whose remedy list the reader cannot act on is
    how a gate becomes advice."""
    _code, report, _payload = _round(monkeypatch, capsys, tmp_path, runs=STOPPED)
    assert "the size was not the problem, and no ceiling was consulted" in report
    assert "**The verdict this round would have overwritten:**" in report
    assert "it was NOT convergence — the cycle stopped with work outstanding" in report
    assert "`--new-cycle` to start a genuinely new cycle" in report
    assert "`--force` does NOT move this gate" in report


def test_force_does_not_override_it(monkeypatch, capsys, tmp_path):
    """`--force` says this diff is worth reading anyway, which is a different
    question. Letting it serve as the answer here would leave the only opt-in
    indistinguishable from the flag people already pass to get past a size refusal —
    and #617's whole failure is a guard that was never consulted."""
    code, report, payload = _round(monkeypatch, capsys, tmp_path, runs=STOPPED,
                                   force=True)
    assert code == 0
    assert payload["reviewed"] is False
    assert payload["preflight"]["verdict"] == "refuse"
    # Not recorded as a forced round either: `forced`/`would_have` are how a
    # verdict says a refusal was CONVERTED into a run, and nothing was converted
    # here. A payload claiming otherwise would say a caller overrode a gate that
    # in fact held.
    assert payload["preflight"]["forced"] is False
    assert payload["preflight"]["would_have"] is None
    assert "`--force` does NOT move this gate" in report


def test_new_cycle_lets_the_round_run_and_says_so_where_nobody_can_skim_past_it(
        monkeypatch, capsys, tmp_path):
    """The opt-in is recorded loudly rather than merely honoured: a round that stepped
    past a terminal verdict looks exactly like any other round, which is the whole
    defect. So the sentence goes above the round summary that would otherwise be the
    first thing read."""
    code, report, payload = _round(monkeypatch, capsys, tmp_path, runs=STOPPED,
                                   new_cycle=True)
    assert code == 0 and payload["reviewed"] is True
    assert "**A previous cycle on this PR was already ENDED.**" in report
    assert "started with `--new-cycle`, so it is a NEW cycle" in report
    assert "That stop was **not** convergence" in report
    assert any("--new-cycle:" in n and "NEW cycle on the same PR" in n
               for n in payload["config_notes"])


def test_the_stepped_past_verdict_rides_in_the_payload_of_the_round_that_ran(
        monkeypatch, capsys, tmp_path):
    """`preflight.prior_cycle` is carried on a `run` verdict too, and `refused` is the
    field that separates the two: renderers branch on it rather than on the record's
    presence, so a `--new-cycle` round publishes what it stepped past without
    claiming it was refused."""
    _code, _report, payload = _round(monkeypatch, capsys, tmp_path, runs=STOPPED,
                                     new_cycle=True)
    pc = payload["preflight"]["prior_cycle"]
    assert pc["refused"] is False and pc["round"] == 2


def test_whether_the_branch_moved_since_the_stop_is_recorded_either_way(
        monkeypatch, capsys, tmp_path):
    """It does not soften the refusal — a fix pass after a stop is the shape #617
    measured, not an answer to it — and it is recorded because a reader deciding
    whether to pass `--new-cycle` wants to know whether anything happened in
    between."""
    _c, still, _p = _round(monkeypatch, capsys, tmp_path, runs=STOPPED, head=HEAD)
    assert "The branch has not moved since." not in still  # refused, so no banner
    _c, _r, unmoved = _round(monkeypatch, capsys, tmp_path, runs=STOPPED)
    assert unmoved["preflight"]["prior_cycle"]["head_moved"] is False

    moved_head = "def4560000000000000000000000000000000000"
    _c, report, moved = _round(monkeypatch, capsys, tmp_path, runs=STOPPED,
                               head=moved_head, new_cycle=True)
    assert moved["preflight"]["prior_cycle"]["head_moved"] is True
    assert "The branch has moved since." in report


def test_a_board_that_could_not_be_asked_lets_the_round_run_and_says_so(
        monkeypatch, capsys, tmp_path):
    """Best-effort, and the two answers must never render alike: this round may be
    continuing a cycle that was told to stop, and "we did not find one" against "we
    could not look" is the distinction a reader needs. The round runs exactly as it
    did before this existed."""
    code, _report, payload = _round(monkeypatch, capsys, tmp_path,
                                    err="connection refused")
    assert code == 0 and payload["reviewed"] is True
    assert any("could not be asked whether an earlier round already ended this cycle"
               in n and "this round ran without that check" in n
               for n in payload["config_notes"])


def test_a_cycle_nobody_stopped_costs_the_round_nothing(monkeypatch, capsys,
                                                        tmp_path):
    """The path every ordinary round takes: a verdict was looked for, none was found,
    and the payload says so by carrying no record rather than by carrying an empty
    one."""
    code, report, payload = _round(monkeypatch, capsys, tmp_path,
                                   runs=(_run_row(1, stopped=False),))
    assert code == 0 and payload["reviewed"] is True
    assert payload["preflight"]["prior_cycle"] is None
    assert "already ENDED" not in report
    assert not [n for n in payload["config_notes"] if "--new-cycle" in n]
