"""Unit tests for the epic driver's pure logic — the parts that don't touch
git/gh/docker: topo-sort, classify (incl. the merged resume signal), run-state
load/save, artifact/green parsing, workspace-trust resolution, model routing.

The --execute side effects (create-worktree, /fix-issue, ff-merge, teardown) are
integration-only and exercised by a live run, not here.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epic  # noqa: E402


def mk(num, stage="implement", title="t", body="", pr=None, pr_state=None, phase=""):
    return epic.IssueWork(
        num=num, title=title, checked=False, issue_state="OPEN",
        pr_number=pr, pr_state=pr_state, stage=stage, body=body, phase=phase)


# --------------------------------------------------------------- classify

def test_classify_open_no_pr_is_implement():
    assert epic.classify({"state": "OPEN"}, None, False) == "implement"


def test_classify_open_pr_is_review():
    assert epic.classify({"state": "OPEN"}, {"state": "OPEN"}, False) == "review"


@pytest.mark.parametrize("issue,pr,checked", [
    ({"state": "CLOSED"}, None, False),
    ({"state": "OPEN"}, {"state": "MERGED"}, False),
    ({"state": "OPEN"}, None, True),
])
def test_classify_done_signals(issue, pr, checked):
    assert epic.classify(issue, pr, checked) == "done"


def test_classify_merged_ancestor_overrides_everything():
    # The P3 resume signal: even an OPEN issue with no PR is done once its branch
    # is an ancestor of the epic branch.
    assert epic.classify({"state": "OPEN"}, None, False, merged=True) == "done"


# --------------------------------------------------------------- toposort

def test_toposort_orders_dependency_before_dependent():
    work = [mk(3), mk(1), mk(2)]
    # edge (a, b) = a depends on b  →  #3 depends on #1, #2 depends on #3
    ordered = [w.num for w in epic.toposort(work, [(3, 1), (2, 3)])]
    assert ordered.index(1) < ordered.index(3) < ordered.index(2)


def test_toposort_stable_without_edges():
    work = [mk(5), mk(2), mk(9)]
    assert [w.num for w in epic.toposort(work, [])] == [5, 2, 9]


def test_toposort_cycle_keeps_all_items():
    work = [mk(1), mk(2)]
    out = epic.toposort(work, [(1, 2), (2, 1)])
    assert sorted(w.num for w in out) == [1, 2]


def test_toposort_ignores_dangling_edges():
    work = [mk(1), mk(2)]
    # edge references #99 which isn't in the worklist — must not raise or drop items
    out = epic.toposort(work, [(1, 99)])
    assert sorted(w.num for w in out) == [1, 2]


# --------------------------------------------------------------- run state

def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(epic, "STATE_DIR", tmp_path)
    st = epic.load_state("demo", 42)
    assert st == {"epic": 42, "repo": "demo", "issues": {}}
    epic.record(st, 100, stage="reviewed", pr=7, branch="feat/issue-100")
    reloaded = epic.load_state("demo", 42)
    assert reloaded["issues"]["100"]["stage"] == "reviewed"
    assert reloaded["issues"]["100"]["pr"] == 7
    assert "ts" in reloaded["issues"]["100"]
    assert "updated" in reloaded


def test_state_tolerates_legacy_list_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(epic, "STATE_DIR", tmp_path)
    (tmp_path / "epic-demo-859.json").write_text(json.dumps({
        "epic": 859, "repo": "demo",
        "issues": [{"num": 860, "stage": "done"}, {"num": 861, "stage": "done"}],
    }))
    st = epic.load_state("demo", 859)
    assert isinstance(st["issues"], dict)
    assert st["issues"]["860"]["stage"] == "done"


def test_state_corrupt_file_resets(tmp_path, monkeypatch):
    monkeypatch.setattr(epic, "STATE_DIR", tmp_path)
    (tmp_path / "epic-demo-1.json").write_text("{not json")
    assert epic.load_state("demo", 1)["issues"] == {}


# --------------------------------------------------------------- pr_green parsing

def _fake_gh(monkeypatch, stdout="", stderr="", rc=0):
    import subprocess
    class P:
        returncode = rc
    def fake_run(*a, **k):
        p = P()
        p.stdout, p.stderr = stdout, stderr
        return p
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_pr_green_all_success(monkeypatch):
    _fake_gh(monkeypatch, stdout=json.dumps([{"bucket": "pass"}, {"bucket": "pass"}]))
    assert epic.pr_green("o/r", 1) == (True, "green")


def test_pr_green_any_fail(monkeypatch):
    _fake_gh(monkeypatch, stdout=json.dumps([{"bucket": "pass"}, {"bucket": "fail"}]), rc=1)
    assert epic.pr_green("o/r", 1) == (False, "red")


def test_pr_green_pending(monkeypatch):
    _fake_gh(monkeypatch, stdout=json.dumps([{"bucket": "pending"}]))
    assert epic.pr_green("o/r", 1) == (False, "pending")


def test_pr_green_no_checks(monkeypatch):
    _fake_gh(monkeypatch, stdout="", stderr="no checks reported on the 'x' branch", rc=1)
    assert epic.pr_green("o/r", 1) == (True, "none")


# --------------------------------------------------------------- trust resolution

def test_workspace_trusted_direct(tmp_path, monkeypatch):
    home = tmp_path
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {"/w/app": {"hasTrustDialogAccepted": True}}}))
    monkeypatch.setattr(epic.Path, "home", classmethod(lambda cls: home))
    assert epic.workspace_trusted("/w/app") is True


def test_workspace_trusted_via_ancestor(tmp_path, monkeypatch):
    home = tmp_path
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {"/w": {"hasTrustDialogAccepted": True},
                      "/w/app": {"hasTrustDialogAccepted": False}}}))
    monkeypatch.setattr(epic.Path, "home", classmethod(lambda cls: home))
    # a worktree sibling under a trusted parent inherits trust
    assert epic.workspace_trusted("/w/app-feat-issue-1") is True


def test_workspace_untrusted(tmp_path, monkeypatch):
    home = tmp_path
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {"/other": {"hasTrustDialogAccepted": True}}}))
    monkeypatch.setattr(epic.Path, "home", classmethod(lambda cls: home))
    assert epic.workspace_trusted("/w/app") is False


def test_workspace_trusted_missing_config_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(epic.Path, "home", classmethod(lambda cls: tmp_path))
    assert epic.workspace_trusted("/anything") is True


# --------------------------------------------------------------- preflight

def test_preflight_bypass_mode_is_clean_regardless_of_trust(monkeypatch):
    monkeypatch.setattr(epic, "workspace_trusted", lambda p: False)
    monkeypatch.setattr(epic, "available_mem_mb", lambda: 8000)
    monkeypatch.setattr(epic, "orphan_container_count", lambda: 0)
    cfg = {"path": "/w", "headless_permission_mode": "bypassPermissions", "epic": {}}
    blockers, _ = epic.preflight(cfg, execute=True)
    assert blockers == []


def test_preflight_untrusted_nonbypass_blocks(monkeypatch):
    monkeypatch.setattr(epic, "workspace_trusted", lambda p: False)
    monkeypatch.setattr(epic, "available_mem_mb", lambda: 8000)
    monkeypatch.setattr(epic, "orphan_container_count", lambda: 0)
    cfg = {"path": "/w", "headless_permission_mode": "acceptEdits", "epic": {}}
    blockers, _ = epic.preflight(cfg, execute=True)
    assert blockers and "not trusted" in blockers[0]


def test_preflight_low_memory_warns(monkeypatch):
    monkeypatch.setattr(epic, "workspace_trusted", lambda p: True)
    monkeypatch.setattr(epic, "available_mem_mb", lambda: 512)
    monkeypatch.setattr(epic, "orphan_container_count", lambda: 0)
    cfg = {"path": "/w", "headless_permission_mode": "bypassPermissions",
           "epic": {"min_free_mb": 2048}}
    _, warnings = epic.preflight(cfg, execute=True)
    assert any("low memory" in w for w in warnings)


def test_preflight_dryrun_noop():
    assert epic.preflight({"path": "/w"}, execute=False) == ([], [])


# --------------------------------------------------------------- model routing

def test_allowed_models_ladder():
    assert epic.allowed_models("opus") == ["sonnet", "opus"]
    assert epic.allowed_models("fable") == ["sonnet", "opus", "fable"]
    assert epic.allowed_models("mystery") == []


def test_clamp_model_over_ceiling_falls_to_ceiling():
    assert epic.clamp_model("fable", "opus") == "opus"
    assert epic.clamp_model("sonnet", "opus") == "sonnet"
    assert epic.clamp_model("", "opus") == "opus"
    assert epic.clamp_model("opus", "unrecognised") == ""


# --------------------------------------------------------------- module boundary

def test_epic_keeps_panel_at_arms_length():
    """epic runs panel.py as a SUBPROCESS on purpose (see run_panel) — they are
    two programs, and epic must not depend on the project. Importing panel for
    one generic helper made all of panel's imports load-bearing for the driver
    at module scope. Shared CLI-failure plumbing belongs in harness_rules, which
    both already import.

    Both import spellings are checked, and the substring test that used to stand
    here caught only one of them: `from panel import stderr_gist` does not
    contain "import panel", so the exact regression this test describes would
    have passed it. The attribute check is the one that cannot be spelled
    around — however the import is written, `panel` would be bound on the
    module."""
    import harness_rules

    assert epic.cli_failure_gist is harness_rules.cli_failure_gist
    assert not hasattr(epic, "panel")
    src = Path(epic.__file__).read_text()
    assert not re.search(r"^\s*(?:import\s+panel\b|from\s+panel\s+import\b)", src, re.M)
    assert "import_module(\"panel\")" not in src and "import_module('panel')" not in src


# --------------------------------------------------------------- triage verdicts
# (_fake_gh fakes any subprocess.run, the triage judge included)

# The real thing, from `agy` 1.1.12: 190 characters, and the remedy is the LAST
# clause. Cut at 120 it ends "…so it was au".
DENIED = ('jetski: no output produced — a tool required the "command" permission '
          "that headless mode cannot prompt for, so it was auto-denied. Add an "
          "allow-rule under permissions.allow in settings.json.")


def test_triage_quotes_the_whole_diagnosis_not_its_first_half(monkeypatch):
    """The gist has to survive intact at the length these messages actually are.
    epic cut it at 120 chars where panel keeps 200, so the half that names the
    remedy fell off exactly the line an operator is left with, and the trimmed
    fixture used elsewhere in this file could not see it."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout="", stderr=DENIED)
    _doable, reason, _impl = epic.triage(mk(1), "opus")
    assert "auto-denied" in reason
    assert "permissions.allow in settings.json" in reason


def test_triage_no_verdict_names_what_stderr_said(monkeypatch):
    """The judge CLI can exit 0 having printed nothing, and explain itself on
    stderr. `doable=None` skips the sub-issue on --execute, so this one line is
    the operator's only account of why — "no verdict" alone is unactionable."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout="", stderr="a tool required the \"command\" "
             "permission that headless mode cannot prompt for, so it was auto-denied")
    doable, reason, impl = epic.triage(mk(1), "opus")
    assert doable is None and impl == ""
    assert "no verdict" in reason and "auto-denied" in reason


def test_triage_no_verdict_falls_back_to_the_exit_code(monkeypatch):
    """A crash with nothing on stderr still beats a bare "no verdict"."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout="", stderr="", rc=2)
    doable, reason, impl = epic.triage(mk(1), "opus")
    assert doable is None and impl == ""
    assert "exited 2" in reason


def test_triage_blames_the_reply_not_the_stderr_when_the_judge_answered(monkeypatch):
    """The mirror error, and the one this branch is most likely to make.

    A judge that replies in PROSE at exit 0 has not failed at running — it failed
    at answering in JSON. Reading stderr anyway pins the blame on whatever the CLI
    happened to log while warming up ("loaded 3 plugins"), which is a confident
    wrong cause on the only line explaining a silently skipped sub-issue. Chatter
    on stderr is the normal state of these CLIs, so this is not a rare shape."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout="I think this one is doable, roughly speaking.",
             stderr="loaded 3 plugins")
    doable, reason, impl = epic.triage(mk(1), "opus")
    assert doable is None and impl == ""
    assert "no JSON in reply" in reason
    assert "plugins" not in reason


def test_triage_bad_verdict_says_what_was_wrong_with_it(monkeypatch):
    """A malformed verdict is the same class of failure as no verdict — same
    silent --execute skip — so it gets the same diagnosis rather than a bare
    "bad verdict". `{...}` matched, so the judge answered: blame the reply."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout='{"doable": True, "reason": unquoted}',
             stderr="loaded 3 plugins")
    doable, reason, impl = epic.triage(mk(1), "opus")
    assert doable is None and impl == ""
    assert "bad verdict" in reason and "malformed JSON" in reason
    assert "plugins" not in reason


def test_triage_bad_verdict_on_a_crash_still_quotes_stderr(monkeypatch):
    """A non-zero exit means the run itself went wrong, so whatever partial
    brace-soup reached stdout is not the story — stderr is."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout='{"doable": tru}',
             stderr="error: the model pin is unusable", rc=1)
    _doable, reason, _impl = epic.triage(mk(1), "opus")
    assert "the model pin is unusable" in reason


def test_triage_refuses_a_verdict_from_a_judge_that_exited_non_zero(monkeypatch):
    """The exit code is checked BEFORE the reply is parsed. A CLI that prints a
    well-formed verdict on its way out and then fails did not rule — accepting
    that JSON would send a sub-issue to an executor on the word of a run that
    crashed, and swallow the stderr saying so. `cli_failure_gist` already applies
    this rule on the failure paths; the success path never saw it."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout=json.dumps(
        {"doable": True, "reason": "looks fine", "model": "sonnet"}),
        stderr="error: the model pin is unusable", rc=1)
    doable, reason, impl = epic.triage(mk(1), "opus")
    assert doable is None and impl == ""
    assert "judge failed" in reason and "the model pin is unusable" in reason
    assert "looks fine" not in reason


def test_triage_names_the_timeout_rather_than_a_bare_judge_error(monkeypatch):
    """A judge that never returned is the same silent --execute skip as one that
    replied badly, so it gets the same courtesy: the cause, not "judge error"."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=epic.TRIAGE_TIMEOUT)

    monkeypatch.setattr(subprocess, "run", boom)
    doable, reason, impl = epic.triage(mk(1), "opus")
    assert doable is None and impl == ""
    assert f"timed out after {epic.TRIAGE_TIMEOUT}s" in reason


def test_triage_names_why_the_judge_could_not_be_launched(monkeypatch):
    """errno and strerror, not the bare class name: "OSError" sends the reader
    looking for a crash that was "Argument list too long"."""
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(*_a, **_k):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(subprocess, "run", boom)
    doable, reason, impl = epic.triage(mk(1), "opus")
    assert doable is None and impl == ""
    assert "could not start" in reason and "Argument list too long" in reason


def test_an_untriaged_sub_issue_is_blocked_rather_than_handed_to_the_executor(
        monkeypatch, capsys):
    """`doable=None` is the ABSENCE of a ruling, not a permissive one.

    Left at stage 'implement' it lands in `pending`, `plan_entry` gives it
    `action: implement`, and the autonomous executor opens a worktree, runs
    /fix-issue and files a PR for an issue no judge ever ruled on — while the
    reason line beside it says it was passed over. Only a confirmed `doable`
    gets executed; everything else waits for a human."""
    monkeypatch.setattr(epic, "load_repo_cfg", lambda name: {
        "name": "r", "github": "acme/r", "path": "/w", "default_branch": "main",
        "_rules_from": "test", "loops": {"issue_executor": True}})
    monkeypatch.setattr(epic, "build_worklist", lambda repo, e: [mk(1), mk(2), mk(3)])
    monkeypatch.setattr(epic, "gh_json", lambda args, repo: {"title": "an epic"})
    verdicts = {1: (None, "untriaged (judge timed out after 300s)", ""),
                2: (False, "needs a licence purchase", ""),
                3: (True, "self-contained", "sonnet")}
    monkeypatch.setattr(epic, "triage", lambda w, model: verdicts[w.num])

    assert epic.run("r", 7, execute=False, max_issues=None, json_out=True,
                    landing="multi") == 0
    out = capsys.readouterr().out
    plan = json.loads(out[out.index("{"):])
    issues = {i["num"]: i for i in plan["issues"]}
    assert issues[1]["stage"] == "blocked" and issues[1]["action"] == "skip-blocked"
    assert issues[2]["action"] == "skip-blocked"
    assert issues[3]["action"] == "implement"
    assert plan["counts"] == {"total": 3, "workable": 1, "blocked": 2, "phases": 1}


def test_an_untriaged_skip_is_not_reported_as_a_doability_ruling(capsys):
    """Both are skipped; only one of them was judged. "NOT agent-doable" is a
    verdict, and claiming one the judge never reached is the same silence in the
    other direction."""
    untriaged = mk(1, stage="blocked")
    untriaged.reason = "untriaged (judge timed out after 300s)"
    epic.work_issue({"path": "/w", "github": "acme/r", "name": "r"},
                    untriaged, execute=False)
    assert "not confirmed doable" in capsys.readouterr().out

    ruled = mk(2, stage="blocked")
    ruled.doable, ruled.reason = False, "needs a licence purchase"
    epic.work_issue({"path": "/w", "github": "acme/r", "name": "r"},
                    ruled, execute=False)
    assert "NOT agent-doable" in capsys.readouterr().out


def test_triage_reads_a_real_verdict_unchanged(monkeypatch):
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout=json.dumps(
        {"doable": True, "reason": "self-contained", "model": "sonnet"}),
        stderr="loaded 3 plugins")
    assert epic.triage(mk(1), "opus") == (True, "self-contained", "sonnet")
