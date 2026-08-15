"""The epic driver saying WHY a sub-issue produced nothing.

Each sub-issue is worked by a headless `claude -p` running a skill, unattended.
The driver checks the artifact afterwards (a PR must exist) but used to run the
agent with no capture at all, so when the artifact was missing the run's own
explanation had already gone nowhere — the operator got "produced no commit and
no PR" and the worktree it happened in was torn down in the `finally`.

Also pinned here: a /review-pr that never ran no longer reports as "reviewed".
That outcome is what lets the driver stack a sub-PR into the epic branch, so
calling it reviewed merges a PR whose findings nobody addressed.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epic  # noqa: E402


def work(num=31, stage="implement", pr=None):
    return epic.IssueWork(num=num, title="a sub-issue", checked=False,
                          issue_state="OPEN", pr_number=pr,
                          pr_state="OPEN" if pr else None, stage=stage,
                          body="", phase="")


def ran(rc=0, out="ok", err=""):
    return subprocess.CompletedProcess(["claude", "-p", "…"], rc, out, err)


def new_state():
    return {"epic": 52, "repo": "thing", "issues": {}}


def arrange(monkeypatch, tmp_path, agent, discovered_pr=None, head_shas=None,
            panelled=True):
    """Neutralise everything work_issue touches except the agent run itself.
    Returns (cfg, calls) where calls records each skill the driver invoked."""
    calls = []
    monkeypatch.setattr(epic, "STATE_DIR", tmp_path / "state")

    def fake_claude(skill_cmd, cwd, perm_mode, model=""):
        calls.append(skill_cmd)
        return agent(skill_cmd) if callable(agent) else agent

    monkeypatch.setattr(epic, "claude", fake_claude)
    monkeypatch.setattr(epic, "fork_point_behind", lambda path, branch, base: "")
    monkeypatch.setattr(epic, "_discover_pr", lambda repo, num: discovered_pr)
    monkeypatch.setattr(epic, "worktree_has_new_commit", lambda wt, base: False)
    monkeypatch.setattr(epic, "worktree_dirty", lambda wt: False)
    monkeypatch.setattr(epic, "teardown_worktree", lambda cfg, branch, wt: None)
    # run_panel reports whether it produced a report; a dead panel leaves
    # /review-pr nothing to act on, which the merge gate has to be able to see.
    monkeypatch.setattr(epic, "run_panel", lambda repo_path, pr: panelled)
    # Default: the review pushed something, so the "pushed nothing" line is only
    # exercised by the test that asks for it.
    shas = iter(head_shas or ["sha-before", "sha-after"])
    monkeypatch.setattr(epic, "pr_head_sha", lambda repo, pr: next(shas, ""))
    monkeypatch.setattr(epic, "git", lambda path, *args:
                        subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    cfg = {"path": str(tmp_path / "repo"), "github": "acme/thing",
           "headless_permission_mode": "acceptEdits", "default_branch": "main",
           "epic": {}}
    return cfg, calls


# ------------------------------------------------------- implement stage

def test_no_pr_records_what_the_agent_said_about_it(monkeypatch, tmp_path, capsys):
    """The state file outlives the worktree, so the reason has to be in it."""
    agent = ran(out="I was denied permission to run `gh`, so I opened no PR.")
    cfg, _ = arrange(monkeypatch, tmp_path, agent)
    state = new_state()

    res = epic.work_issue(cfg, work(), execute=True, state=state)

    assert res.outcome == "failed"
    assert "produced no commit and no PR" in res.detail
    assert "denied permission to run `gh`" in res.detail
    assert "denied permission" in state["issues"]["31"]["lastAction"]


def test_a_silently_failed_run_is_named_as_a_failure_not_just_a_missing_pr(
        monkeypatch, tmp_path, capsys):
    """exit 0, nothing on stdout — the #19 shape. Said once as it happens, and
    again in the detail that survives the run."""
    cfg, _ = arrange(monkeypatch, tmp_path, ran(out="", err="API Error: overloaded"))

    res = epic.work_issue(cfg, work(), execute=True, state=new_state())

    assert "/fix-issue exited 0 having printed nothing" in capsys.readouterr().out
    assert "exited 0 having printed nothing (API Error: overloaded)" in res.detail


def test_a_pr_that_did_appear_is_reviewed_even_if_the_run_exited_badly(
        monkeypatch, tmp_path, capsys):
    """A run can open its PR and then trip on the way out. The exit is worth a
    line; it is not worth discarding the artifact."""
    def agent(skill_cmd):
        return (ran(rc=1, out="pushed", err="teardown")
                if skill_cmd.startswith("/fix-issue") else ran(out="Addressed 1 finding."))

    cfg, calls = arrange(monkeypatch, tmp_path, agent, discovered_pr=42)

    res = epic.work_issue(cfg, work(), execute=True, state=new_state())

    assert res.outcome == "reviewed"
    assert res.pr == 42
    assert "/fix-issue exited 1" in capsys.readouterr().out
    assert calls == ["/fix-issue 31 --base main", "/review-pr 42"]


# ---------------------------------------------------------- review stage

def test_a_review_that_never_ran_does_not_report_as_reviewed(
        monkeypatch, tmp_path, capsys):
    """'reviewed' is the outcome that lets the driver stack the sub-PR — so a
    /review-pr that was denied its tools must not produce it.

    The head is unchanged, which is what a denied agent leaves behind — and the
    two facts together are the failure. A run that failed AFTER pushing is a
    different case, covered below."""
    cfg, _ = arrange(monkeypatch, tmp_path, ran(rc=1, err="Error: tool use denied"),
                     head_shas=["same-sha", "same-sha"])
    state = new_state()

    res = epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=state)

    assert res.outcome == "failed"
    assert res.pr == 99
    assert "findings unaddressed" in res.detail
    assert state["issues"]["31"]["stage"] == "failed"


def test_a_review_that_pushed_nothing_is_reported_but_not_failed(
        monkeypatch, tmp_path, capsys):
    """Finding nothing to fix is legitimate — by the last round it is the point —
    so this is reported with the agent's account, not turned into a failure."""
    cfg, _ = arrange(monkeypatch, tmp_path, ran(out="No findings needed a change."),
                     head_shas=["same-sha", "same-sha"])
    state = new_state()

    res = epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=state)

    assert res.outcome == "reviewed"
    out = capsys.readouterr().out
    assert "pushed nothing" in out and "PR #99 is unverified" in out
    assert "It said: No findings needed a change." in out
    # Reported as reviewed, but NOT as something the auto-merge path may consume:
    # "found nothing to fix" and "was stopped from fixing anything" are the same
    # shape from out here, and only one of them is safe to stack.
    assert res.verified is False
    assert "unverified" in state["issues"]["31"]["lastAction"]


def test_an_unreadable_head_sha_is_not_read_as_a_review_that_did_nothing(
        monkeypatch, tmp_path, capsys):
    """"" means 'could not tell'. Reporting it as 'pushed nothing' would invent a
    finding out of a failed gh call."""
    cfg, _ = arrange(monkeypatch, tmp_path, ran(out="Addressed 2 findings."),
                     head_shas=["", ""])

    epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=new_state())

    assert "pushed nothing" not in capsys.readouterr().out


def test_a_real_review_still_reports_reviewed(monkeypatch, tmp_path):
    cfg, _ = arrange(monkeypatch, tmp_path, ran(out="Addressed 3 findings."))
    state = new_state()

    res = epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=state)

    assert res.outcome == "reviewed"
    assert state["issues"]["31"]["stage"] == "reviewed"


# ---------------------------------------------------------------- triage

def _judge(monkeypatch, proc):
    monkeypatch.setattr(epic.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: proc)


def test_an_unrunnable_judge_says_so_rather_than_just_untriaged(monkeypatch):
    """Untriaged means SKIPPED on --execute, so 'no verdict' has to distinguish
    a judge that disagreed from one that never spoke."""
    _judge(monkeypatch, ran(rc=1, err="Error: model 'opus' is not available"))

    doable, reason, model = epic.triage(work(), "opus")

    assert doable is None
    assert "model 'opus' is not available" in reason
    assert model == ""


def test_a_judge_that_answered_without_a_verdict_quotes_the_answer(monkeypatch):
    _judge(monkeypatch, ran(out="I cannot assess this issue: its body is empty."))

    doable, reason, _ = epic.triage(work(), "opus")

    assert doable is None
    assert "its body is empty" in reason


def test_a_real_verdict_is_unaffected(monkeypatch):
    _judge(monkeypatch, ran(out='{"doable": true, "reason": "clear scope", "model": ""}'))

    doable, reason, _ = epic.triage(work(), "opus")

    assert doable is True
    assert reason == "clear scope"


def test_a_review_that_pushed_and_then_tripped_keeps_its_work(
        monkeypatch, tmp_path, capsys):
    """The implement stage two blocks up already refuses to judge by exit code
    alone — "a run can open its PR and then trip on the way out. The exit is
    worth a line; it is not worth discarding the artifact." The review stage was
    judging by exit code alone, and it read the head SHA only AFTER deciding.

    So a /review-pr that read the panel report, pushed three fix commits and then
    died on teardown or an MCP disconnect was recorded `failed`, which without
    --keep-going aborts the whole epic. The contradicting evidence was one
    already-written call away."""
    cfg, _ = arrange(monkeypatch, tmp_path,
                     ran(rc=1, out="Pushed 3 fixes.", err="MCP server disconnected"),
                     head_shas=["before", "after"])
    state = new_state()

    res = epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=state)

    assert res.outcome == "reviewed" and res.verified is True
    assert "keeping the work" in capsys.readouterr().out
    assert state["issues"]["31"]["stage"] == "reviewed"


def test_a_dead_panel_leaves_the_review_unverified(monkeypatch, tmp_path, capsys):
    """A panel that died leaves /review-pr nothing to act on — but /review-pr can
    still print a plausible no-op reply, pass agent_failure, and record
    `reviewed`, which is the outcome that lets the sub-PR be stacked. A PR then
    reaches the merge gate having had no findings generated at all. Warning about
    it in the log is not something the gate can act on."""
    cfg, _ = arrange(monkeypatch, tmp_path, ran(out="Nothing to address."),
                     head_shas=["before", "after"], panelled=False)
    state = new_state()

    res = epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=state)

    assert res.outcome == "reviewed" and res.verified is False
    assert "the panel produced no report" in capsys.readouterr().out


def test_an_unreadable_head_sha_stays_unknown_rather_than_counting_as_a_push(
        monkeypatch, tmp_path, capsys):
    """`pushed = not (before and after and before == after)` was true whenever
    either lookup returned the documented "could not tell" empty string, so a
    failed `gh` call was recorded as a plain `reviewed` and fed the auto-merge
    path. pr_head_sha's own docstring says `""` means could not tell, never
    nothing changed — and the caller collapsed unknown into the confident
    branch."""
    cfg, _ = arrange(monkeypatch, tmp_path, ran(out="Addressed the findings."),
                     head_shas=["before", ""])
    state = new_state()

    res = epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=state)

    assert res.outcome == "reviewed" and res.verified is False
    assert "the head SHA could not be read" in capsys.readouterr().out
