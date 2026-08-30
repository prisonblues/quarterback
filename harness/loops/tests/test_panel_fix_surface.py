"""#619: what SURFACE the last fix pass opened, which is not the same as its size.

Every number downstream of a fix pass counts lines or findings — `max_fix_growth` at
3.0x, `max_fix_growth_chars` at 30,000, `fix_injection` at 0.5 — and fifteen lines
added to two nginx templates nobody had reviewed is small by all three. On
`prisonblues/lexray#1780` round 3's pass touched twelve files and **seven of them had
never been in front of a reviewer**; both of the cycle's later P1s were in that new
surface, and ten of the PR's files arrived from a fix pass rather than from the change
under review.

So the round measures it: the files the fix range touched, minus the files earlier
rounds of this cycle recorded. It is REPORTED and gates nothing — #67's
instrument-before-gate rule, and the dial that would bind it (`max_fix_new_files`) is
a number that would be invented today with its argument written afterwards.

The distinction this file spends most of its assertions on is `None` against zero.
Round 1 has no fix pass to read and a rewritten branch has no readable range; a
payload publishing `count: 0` for those would claim a pass opened no new files when
what happened is that nobody looked. Both are legitimate answers and they are not the
same answer.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402  — `sh`, the seam every stub here replaces
import panel_scope  # noqa: E402  — the compare round trips a scoped round makes
from conftest import gh_stub  # noqa: E402

CFG = {"github": "acme/board", "path": "/nonexistent/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
       "review_panel": {}}


def _diff(*files):
    """A diff touching each named path, in the shape `_fix_pass_files` reads."""
    return "".join(f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n"
                   f"@@ -1,0 +1,1 @@\n+line\n" for f in files)


# ------------------------------------------------------- the measurement itself

def test_the_files_a_pass_opened_are_the_ones_no_earlier_round_had_seen():
    """A set difference, and the whole feature is that somebody takes it. `files` is
    everything the pass touched and `new_files` is the part that matters — the second
    is what a reviewer never had in front of it, and `prior_files` is the size of the
    left-hand side so a reader can see what the difference was taken against."""
    got = panel.fix_surface(_diff("app/sync.py", "nginx/site.conf"),
                            {"app/sync.py", "app/read.py"})
    assert got == {"files": ["app/sync.py", "nginx/site.conf"],
                   "new_files": ["nginx/site.conf"], "count": 1, "prior_files": 2}


def test_a_pass_that_stayed_inside_the_change_measures_a_genuine_ZERO():
    """The answer that has to be distinguishable from "not measured", and the healthy
    one: the pass touched files and every one of them had already been reviewed. A
    payload that rendered this the same way it renders round 1 would make the good
    case indistinguishable from the blind one."""
    got = panel.fix_surface(_diff("app/sync.py"), {"app/sync.py", "app/read.py"})
    assert got["count"] == 0 and got["new_files"] == []
    assert got["files"] == ["app/sync.py"] and got["prior_files"] == 2


def test_a_round_with_no_fix_range_to_read_measured_NOTHING():
    """Round 1 has no pass before it, and a rebased branch has no readable range —
    the same conditions `unrefereed_fix` is absent under. "The pass opened no new
    files" is a claim about a fix pass, and only one of those two things is ever true
    of round 1."""
    assert panel.fix_surface(None, {"app/sync.py"}) is None
    assert panel.fix_surface("", {"app/sync.py"}) is None


def test_a_cycle_with_no_readable_EARLIER_round_measured_nothing_either():
    """The third absence, and the one that is easiest to get wrong in the direction
    that flatters nobody: with no prior file set every file the pass touched reads as
    new, so a pass that opened nothing would be reported as having opened everything.
    An unmeasurable pass must not be reported as a pass that opened nothing, and it
    must not be reported as one that opened the world either."""
    assert panel.fix_surface(_diff("app/sync.py"), set()) is None


def test_a_chunk_with_no_path_is_dropped_rather_than_named():
    """`_fix_pass_files` drops the preamble and any header it cannot parse, which is
    the harmless direction here: a chunk nobody can attribute to a path cannot be a
    file the pass opened, and counting one would put an unnamable entry in a list
    printed to a human."""
    noise = "some preamble nobody wrote as a diff\n" + _diff("app/new.py")
    got = panel.fix_surface(noise, {"app/sync.py"})
    assert got["files"] == ["app/new.py"] and got["new_files"] == ["app/new.py"]


# --------------------------------------------- how the payload carries the answer

def test_the_measurement_is_published_whole_and_unchanged():
    """`round_stop` normalises rather than recomputes: the four fields arrive on a
    fixed contract and are published under it, so the payload and the printed line
    are one measurement rather than two derivations that can disagree."""
    surface = {"files": ["a.py", "b.py"], "new_files": ["b.py"], "count": 1,
               "prior_files": 3}
    d = panel.round_stop(2, 5, [], [], [], surface=surface)
    assert d["fix_surface"] == surface


def test_a_genuine_zero_and_a_missing_measurement_do_not_render_alike():
    """The distinction the whole block turns on, asserted where a consumer reads it.
    `null` is "nobody looked"; a mapping with `count: 0` is "the pass stayed inside
    the change under review", which is a real and good answer about a real fix
    pass."""
    zero = panel.round_stop(2, 5, [], [], [], surface={
        "files": ["a.py"], "new_files": [], "count": 0, "prior_files": 4})
    assert zero["fix_surface"]["count"] == 0
    assert panel.round_stop(1, 5, [], [], [])["fix_surface"] is None
    assert panel.round_stop(2, 5, [], [], [], surface=None)["fix_surface"] is None


@pytest.mark.parametrize("nothing", [None, {}, [], "app/new.py", 3,
                                     {"files": ["a.py"]}])
def test_anything_that_states_no_count_and_no_file_list_is_not_a_measurement(nothing):
    """A mapping carrying neither `count` nor `new_files` is an absent measurement
    rather than an empty one, and so is anything that is not a mapping at all. The
    fourth case is the one worth the parametrisation: `{"files": [...]}` looks like an
    answer and states nothing about what was NEW, which is the only question here."""
    assert panel.round_stop(2, 5, [], [], [], surface=nothing)["fix_surface"] is None


def test_a_count_the_caller_did_not_state_is_taken_from_the_file_list():
    """One number, derived where it was not given, so a caller cannot publish a count
    that disagrees with the list beside it."""
    d = panel.round_stop(2, 5, [], [], [],
                         surface={"new_files": ["a.py", "b.py"]})
    assert d["fix_surface"] == {"files": [], "new_files": ["a.py", "b.py"],
                                "count": 2, "prior_files": None}


def test_it_gates_nothing_in_either_direction():
    """#67's rule, enforced rather than documented. The same round with a large new
    surface and with none has to reach the same verdict — nothing here reads this
    field to move `stop`, `confident` or the disposal, and the decision between a dial
    and report-only has not been taken."""
    wide = {"files": [f"f{i}.py" for i in range(12)],
            "new_files": [f"f{i}.py" for i in range(7)], "count": 7, "prior_files": 2}
    with_surface = panel.round_stop(2, 5, [], [], [], surface=wide)
    without = panel.round_stop(2, 5, [], [], [], surface=None)
    assert with_surface["stop"] == without["stop"] is True
    assert with_surface["confident"] == without["confident"] is True
    assert with_surface["converged"] == without["converged"] is True
    assert with_surface["veto"] == without["veto"] == []
    assert "surface" not in with_surface["reason"]


# ----------------------------------------------------------- through a whole round

def _compare(*files):
    """The compare body `_fix_range_diff` reads the fix range out of: `status`, and one
    entry per file with a patch on it. `ahead` is the linear case — a branch that only
    grew between rounds — which is the only one that HAS a fix range to attribute."""
    return json.dumps({"status": "ahead",
                       "files": [{"filename": f, "patch": "@@ -1,0 +1,1 @@\n+line"}
                                 for f in files]})


def _round(monkeypatch, capsys, tmp_path, *, round_no=1, baseline=(), fix=(),
           name="r"):
    """One panel run whose fix range touched `fix`. Round 2 with a baseline is what
    makes the round ATTRIBUTABLE, which is the condition the measurement is taken
    under — round 1 has no pass to read. The head moves per round for the same
    reason: an unchanged head is "no commit landed between rounds", which is a range
    that does not exist rather than one that opened nothing. It is spelled as a real
    40-character hex sha because `load_baseline` validates it and drops an anchor it
    cannot read — which would blind the range without blinding the round, and leave
    this file testing the absence path four times over."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: CFG)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        # `files` is what an earlier round RECORDS as `changed_files`, and that list
        # is the left-hand side of the whole set difference — a round that stated no
        # file list gives the next one nothing to measure against.
        meta={"title": "fix: a real bug", "additions": 3, "deletions": 1,
              "headRefName": "h", "headRefOid": f"{round_no:040d}",
              "files": [{"path": "app/sync.py", "additions": 3, "deletions": 1}]},
        diff=_diff("app/sync.py"),
        compare=_compare(*(fix or ("app/sync.py",)))))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, []))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate",
                        lambda *a, **k: ([], None, panel.CoverageRuling()))
    out = tmp_path / f"{name}{round_no}.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=round_no, baseline=list(baseline), max_rounds=5,
                     scope="pr") == 0
    return capsys.readouterr().out, json.loads(out.read_text()), str(out)


def test_round_one_reports_no_surface_at_all(monkeypatch, capsys, tmp_path):
    """There is no pass before it, so there is no measurement — and the report says
    nothing rather than printing a line that reads "0 of 0 files"."""
    report, payload, _ = _round(monkeypatch, capsys, tmp_path)
    assert payload["round_stop"]["fix_surface"] is None
    assert "New surface in the last fix pass" not in report


def test_a_pass_that_opened_a_file_no_round_had_read_is_named_in_the_report(
        monkeypatch, capsys, tmp_path):
    """End to end, because the measurement is only worth anything if the file set an
    earlier round recorded is the one this round's difference is taken against. The
    report names the files: a count on its own tells a reader that something happened
    and not where to look."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path)
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[r1],
                                fix=("app/sync.py", "nginx/site.conf"))
    got = payload["round_stop"]["fix_surface"]
    assert got["new_files"] == ["nginx/site.conf"] and got["count"] == 1
    assert "**New surface in the last fix pass:** it touched 2 file(s)" in report
    assert "**1 of them had never been in front of a reviewer**" in report
    assert "`nginx/site.conf`" in report
    assert "Reported, not a threshold — nothing stops on this (#67)." in report


def test_a_pass_that_stayed_inside_the_change_says_so_rather_than_saying_nothing(
        monkeypatch, capsys, tmp_path):
    """The zero, printed. A round that measured the pass and found it disciplined is
    evidence about that pass, and dropping the line would make it indistinguishable
    from the round that could not look."""
    _r1, _first, r1 = _round(monkeypatch, capsys, tmp_path)
    report, payload, _ = _round(monkeypatch, capsys, tmp_path, round_no=2,
                                baseline=[r1], fix=("app/sync.py",))
    assert payload["round_stop"]["fix_surface"]["count"] == 0
    assert ("none of them are new — the pass stayed inside the change under review"
            in report)
