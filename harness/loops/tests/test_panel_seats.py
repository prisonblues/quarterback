"""The panel's seats: where a reviewer runs, and what a run says when it loses one.

Two halves of the same defect (#68), which is #19's disease one level up. #19 stopped
a REVIEWER that produced nothing from reading as a reviewer that found nothing. This
is the PANEL: a run with half its seats empty was presented identically to a full one.

The first half is why a seat goes missing. `run_cli` ran every reviewer with no
`cwd=`, so each inherited whatever directory the panel process happened to be started
from — ambient state nothing configured, nothing recorded and nothing could reproduce.
On PR #64 codex exited 1 with "Not inside a trusted directory and
--skip-git-repo-check was not specified" while two panels launched in the same second
ran it fine; those were started from inside a checkout and that one from a scratch
directory under /tmp. Pinning the cwd to the repo under review satisfies codex's check
by construction, which is why no `--skip-git-repo-check` appears anywhere here.

The second half is what the report says when it happens anyway — a seat can still be
lost to a timeout, a quota, or a model pin the CLI refuses. It has to say so above the
findings, and it has to stop "no finding earned ⋆consensus" reading the same as "there
was nobody to agree with".
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

REPO = "/tmp/acme-board"

# Two seats, so a lost one is a DEGRADED panel rather than the whole panel. A
# one-seat config would conflate the two and is tested separately below.
TWO_SEAT_CFG = {"github": "acme/board", "path": REPO,
                "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                              "codex": {"enabled": True, "model": "", "effort": ""}},
                "review_panel": {}}
ONE_SEAT_CFG = {"github": "acme/board", "path": REPO,
                "reviewers": {"claude": {"enabled": True, "model": "sonnet"}},
                "review_panel": {}}


# ---------------------------------------------------------------- where a seat runs

def test_a_reviewer_runs_where_the_panel_says_not_where_the_shell_was(monkeypatch):
    """The fix for the lost seat: the repo under review reaches the process.

    Asserted on the kwargs `subprocess.run` is actually called with, because every
    layer above it can hold a correct path and still leave the CLI inheriting the
    caller's shell — which is precisely how this shipped."""
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    panel.review_llm("claude", "sonnet", "review this", "", REPO)
    assert seen["cwd"] == REPO


def test_the_judge_runs_there_too(monkeypatch):
    """The judge is a headless CLI on the same host with the same exposure. It is
    also the seat whose loss is worst — a judge that dies takes every finding
    through unadjudicated — so leaving it on the inherited cwd would have kept the
    defect in the one place it costs most."""
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    f = panel.Finding("claude", "P1", "a.py", 1, "title", "detail")
    panel.adjudicate([[f]], "diff", "sonnet", 34, cwd=REPO)
    assert seen["cwd"] == REPO


def test_a_whole_run_hands_every_seat_the_repo_it_resolved(monkeypatch, tmp_path,
                                                           capsys):
    """End to end, because `run_cli` could always ACCEPT a cwd — what was missing
    was every caller passing one. The path asserted is the repo the run resolved,
    not a literal: a panel that pinned some other directory would be reproducible
    and still wrong."""
    got = {}
    _stub_panel(monkeypatch, cfg=TWO_SEAT_CFG)

    def recording_review(name, model, prompt, effort="", cwd=None):
        got[name] = cwd
        return panel.ReviewerRun([], None, 10, [])

    def recording_judge(clusters, diff, model, pr, budget=None, coverage=None,
                        cwd=None):
        got["judge"] = cwd
        return ([], None, "")

    monkeypatch.setattr(panel, "review_llm", recording_review)
    monkeypatch.setattr(panel, "adjudicate", recording_judge)
    assert panel.run("board", 34, post=False, record=False) == 0
    capsys.readouterr()
    assert got == {"claude": REPO, "codex": REPO, "judge": REPO}


# ---------------------------------------------------------------- what a lost seat says

def _stub_panel(monkeypatch, findings=None, cfg=TWO_SEAT_CFG, runs=None):
    """Every process a run would spawn, replaced.

    `runs` maps a reviewer name to the :class:`ReviewerRun` it should return, which
    is how a seat is made to go missing without a CLI being involved."""
    if findings is None:
        findings = [panel.Finding("claude", "P3", "a.py", 3, "unused import")]
    monkeypatch.setattr(panel, "load_repo_cfg", lambda name: cfg)
    monkeypatch.setattr(panel, "sh", lambda args, **kw: (
        json.dumps({"title": "feat: x", "additions": 3, "deletions": 1,
                    "baseRefName": "main", "headRefName": "feat/x", "headRefOid": "abc"})
        if args[:3] == ["gh", "pr", "view"] else "diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", _confirm_everything)

    def review(name, *a, **k):
        if runs and name in runs:
            return runs[name]
        return panel.ReviewerRun(list(findings), None, 10, [])
    monkeypatch.setattr(panel, "review_llm", review)


def _confirm_everything(clusters, diff, model, pr, budget=None, coverage=None,
                        cwd=None):
    flat = [f for grp in clusters for f in grp]
    return ([panel.Canonical(id=panel._finding_id(pr, i + 1), severity=f.severity,
                             file=f.file, line=f.line, synthesis=f.title,
                             verdict="confirmed", detail=f.detail, reported_by=[f],
                             rationale="real")
             for i, f in enumerate(flat)], None, "")


def _report(monkeypatch, capsys, cfg=TWO_SEAT_CFG, runs=None, findings=None):
    _stub_panel(monkeypatch, findings=findings, cfg=cfg, runs=runs)
    assert panel.run("board", 34, post=False, record=False) == 0
    return capsys.readouterr().out


def test_a_panel_that_lost_a_seat_says_so_above_the_findings(monkeypatch, capsys):
    """#64's report read "LLM reviewers ran: claude (opus)" and then laid out 23
    findings exactly as a full panel would. The seat count is the fact that makes
    those two reports different artifacts, and it was nowhere a reader looks."""
    report = _report(monkeypatch, capsys, runs={
        "codex": panel.ReviewerRun(skip="codex: timed out after 1800s")})
    assert "1 of 2 configured" in report
    assert "panel degraded" in report
    # The existing per-seat reason survives alongside the panel-level statement:
    # "which seat" and "how weak is this review" are different questions.
    assert "timed out after 1800s" in report


def test_a_lone_reviewer_says_no_consensus_was_POSSIBLE(monkeypatch, capsys):
    """The distinction #68 is named for. ⋆consensus takes two reviewers, so on a
    panel of one its absence is structural — but it renders exactly like a panel
    where two reviewers read the same code and neither backed the other. A reader
    takes the second meaning, which is the pessimistic reading of a review that
    never got the chance to be pessimistic."""
    report = _report(monkeypatch, capsys, runs={
        "codex": panel.ReviewerRun(skip="codex: CLI absent")})
    assert "no ⋆consensus is possible" in report
    assert "sole reviewer, no second opinion" in report
    assert "⋆consensus)" not in report


def test_a_full_panel_says_none_of_it(monkeypatch, capsys):
    """The other half, and the one that decides whether any of this is readable:
    a caveat that fires on healthy runs is noise, and a reader who learns to skip
    it has lost the degraded case too."""
    report = _report(monkeypatch, capsys)
    assert "2 of 2 configured" in report
    assert "panel degraded" not in report
    assert "no ⋆consensus is possible" not in report
    assert "sole reviewer" not in report


def test_a_deliberate_single_seat_panel_is_still_told_it_has_no_second_opinion(
        monkeypatch, capsys):
    """A repo configured for one reviewer lost nothing, so it is NOT degraded —
    but its findings are just as unchallenged as the degraded panel's, and the
    consensus signal is just as unavailable. The two notes are separate for this
    case: conflating them would either cry degradation at a run that is working as
    configured, or stay silent about a review nobody corroborated."""
    report = _report(monkeypatch, capsys, cfg=ONE_SEAT_CFG)
    assert "1 of 1 configured" in report
    assert "panel degraded" not in report
    assert "no ⋆consensus is possible" in report
