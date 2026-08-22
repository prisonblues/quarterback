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

import json
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
            panelled=True, found=2):
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
    # run_panel reports whether it produced a report AND how many findings it
    # confirmed. A dead or skipped panel leaves /review-pr nothing to act on; a
    # report of ZERO findings makes "the reviewer pushed nothing" the expected
    # outcome rather than an ambiguous one. Default 2, so the tests that care
    # about the pushed-nothing ambiguity get it without asking.
    monkeypatch.setattr(epic, "run_panel",
                        lambda repo_path, pr: (panelled, found if panelled else None))
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

    doable, reason, model, _cls = epic.triage(work(), "opus")

    assert doable is None
    assert "model 'opus' is not available" in reason
    assert model == ""


def test_a_judge_that_answered_without_a_verdict_quotes_the_answer(monkeypatch):
    _judge(monkeypatch, ran(out="I cannot assess this issue: its body is empty."))

    doable, reason, _, _cls = epic.triage(work(), "opus")

    assert doable is None
    assert "its body is empty" in reason


def test_a_real_verdict_is_unaffected(monkeypatch):
    _judge(monkeypatch, ran(out='{"doable": true, "reason": "clear scope", "model": ""}'))

    doable, reason, _, _cls = epic.triage(work(), "opus")

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
    already-written call away.

    **`verified` is now False on this path, and that assertion is the fix to a
    bug this test used to hold in place.** Keeping the artifact and calling the
    review verified are two different decisions, and the branch made both by
    making neither — it printed its line and fell through, so `why` was never set.
    A reviewer that fixed the first of five findings and then died therefore
    auto-merged with four unaddressed, which is exactly the case the unverified
    flag exists for. The work survives; the merge gate is not cleared by it."""
    cfg, _ = arrange(monkeypatch, tmp_path,
                     ran(rc=1, out="Pushed 3 fixes.", err="MCP server disconnected"),
                     head_shas=["before", "after"])
    state = new_state()

    res = epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=state)

    assert res.outcome == "reviewed", "the work must not be discarded"
    assert res.verified is False, "a partial review must not clear the merge gate"
    out = capsys.readouterr().out
    assert "keeping the work" in out
    assert "may have stopped partway" in out
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


def test_a_clean_sub_pr_is_verified_and_does_not_halt_the_stack(
        monkeypatch, tmp_path, capsys):
    """The happy path, and the one the first version of this gate broke.

    A panel that finds nothing, followed by a reviewer that correctly changes
    nothing, is the system working — by the last round it is the point. But
    "pushed nothing" was read as unverified on its own, so an integration epic
    halted on its first CLEAN sub-PR and demanded a human for the outcome it was
    hoping for. "Pushed nothing" is only ambiguous when there was something to
    push, which is what the findings count settles."""
    cfg, _ = arrange(monkeypatch, tmp_path, ran(out="No findings needed a change."),
                     head_shas=["same-sha", "same-sha"], found=0)
    state = new_state()

    res = epic.work_issue(cfg, work(stage="review", pr=99), execute=True, state=state)

    assert res.outcome == "reviewed" and res.verified is True
    assert "unverified" not in capsys.readouterr().out
    assert state["issues"]["31"]["lastAction"] == "reviewed"


def test_a_panel_that_skipped_is_not_a_panel_that_reviewed(monkeypatch, tmp_path):
    """`panel.py` exits 0 on a configured title-pattern skip and on other no-op
    paths, so reading rc==0 as "a report exists" let a SKIPPED panel present as a
    reviewed one — and the sub-PR then cleared the merge gate having had no
    findings generated at all, which is exactly what the gate exists to stop.

    The answer now comes from the JSON payload, which only exists when the panel
    actually produced one and cannot be faked by an exit status."""
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        # Exit 0, writing nothing — a skip.
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(epic.subprocess, "run", fake_run)
    reported, found = epic.run_panel(str(tmp_path), 99)

    assert reported is False and found is None
    assert any("--json-file" in a for a in calls[0]), "asks for the artefact"


def _reviewed(**over) -> str:
    """A payload from a run that ACTUALLY reviewed, as panel.py emits one.

    Spelled out rather than defaulted, because the difference between this and
    `{"to_fix": [...]}` is the entire subject of the gate: a skipped run writes a
    payload too, with these keys at their `_payload_defaults()` values, and the
    old fixtures modelled "a payload exists" as sufficient — which is precisely
    the premise three rounds of review kept defeating."""
    return json.dumps({"reviewed": True, "skip_reason": None, "judged": True,
                       "reviewers_ran": ["claude (opus)"], "to_fix": [], **over})


def test_a_panel_that_wrote_a_report_reports_its_findings(monkeypatch, tmp_path):
    def fake_run(args, **kw):
        path = args[args.index("--json-file") + 1]
        Path(path).write_text(_reviewed(to_fix=[{"id": "F01"}, {"id": "F02"}]))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(epic.subprocess, "run", fake_run)
    assert epic.run_panel(str(tmp_path), 99) == (True, 2)


def test_a_panel_that_ruled_and_then_tripped_keeps_its_findings(monkeypatch, tmp_path):
    """A report AND a bad exit: the run got far enough to rule and then fell over
    on the way out. The findings are real and are not thrown away."""
    def fake_run(args, **kw):
        path = args[args.index("--json-file") + 1]
        Path(path).write_text(_reviewed(to_fix=[{"id": "F01"}]))
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(epic.subprocess, "run", fake_run)
    assert epic.run_panel(str(tmp_path), 99) == (True, 1)


# ---- the premise itself: what counts as "a review happened" -------------------
#
# Three rounds of review on this file each replaced one proxy with another — the
# exit code, then the push, then the payload's existence — and panel.py defeats
# all three on the same skip path. These pin the answer that has no fourth proxy:
# the panel's own account of what it did.

def _panel_writing(payload: str):
    def fake_run(args, **kw):
        Path(args[args.index("--json-file") + 1]).write_text(payload)
        return subprocess.CompletedProcess(args, 0)
    return fake_run


def test_a_SKIPPED_panel_is_not_a_review(monkeypatch, tmp_path):
    """The bug r3 found, and the reason the artefact cannot be the evidence.

    panel.py's title-pattern skip writes a payload and exits 0 (`skipped_payload`
    -> `write_payload` -> `finish`), and `_payload_defaults()` gives it
    `to_fix: []`. So the old gate saw a report with zero findings, concluded the
    reviewer had nothing to fix, and cleared a PR that no reviewer had read."""
    monkeypatch.setattr(epic.subprocess, "run", _panel_writing(json.dumps({
        "reviewed": False, "skip_reason": "title matches skip pattern /^chore/",
        "judged": False, "reviewers_ran": [], "to_fix": []})))
    assert epic.run_panel(str(tmp_path), 99) == (False, None)


def test_a_NO_OP_panel_is_not_a_review_either(monkeypatch, tmp_path):
    """`skip_reason` is the loud case. `reviewed: False` with no reason is the
    quiet one — every other exit-0 path that produced no review — and the gate
    must not need to enumerate them."""
    monkeypatch.setattr(epic.subprocess, "run", _panel_writing(json.dumps({
        "reviewed": False, "skip_reason": None, "judged": False,
        "reviewers_ran": [], "to_fix": []})))
    assert epic.run_panel(str(tmp_path), 99) == (False, None)


def test_a_panel_whose_every_SEAT_was_empty_is_not_a_review(monkeypatch, tmp_path):
    """`reviewed: True` and nobody filed. A round can start, mark itself reviewed
    and lose every seat to quota or a bad model pin — and zero findings from zero
    reviewers is indistinguishable from a clean PR unless the seat list is read.
    That is #68's disease reaching the merge gate."""
    monkeypatch.setattr(epic.subprocess, "run", _panel_writing(json.dumps({
        "reviewed": True, "skip_reason": None, "judged": True,
        "reviewers_ran": [], "to_fix": []})))
    assert epic.run_panel(str(tmp_path), 99) == (False, None)


def test_a_real_review_that_found_nothing_still_counts(monkeypatch, tmp_path):
    """The case the gate must NOT refuse, and the reason it reads four keys rather
    than just counting findings. A clean PR reviewed by a full panel produces zero
    findings, and by the last round that is the expected outcome — treating it as
    suspect halts an integration epic on its first clean sub-PR."""
    monkeypatch.setattr(epic.subprocess, "run", _panel_writing(_reviewed()))
    assert epic.run_panel(str(tmp_path), 99) == (True, 0)
