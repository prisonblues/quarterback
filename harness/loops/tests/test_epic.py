"""Unit tests for the epic driver's pure logic — the parts that don't touch
git/gh/docker: topo-sort, classify (incl. the merged resume signal), run-state
load/save, artifact/green parsing, workspace-trust resolution, model routing.

The --execute side effects (create-worktree, /fix-issue, ff-merge, teardown) are
integration-only and exercised by a live run, not here.
"""
from __future__ import annotations

import json
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


# --------------------------------------------------------------- triage verdicts
# (_fake_gh fakes any subprocess.run, the triage judge included)

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
    _doable, reason, _impl = epic.triage(mk(1), "opus")
    assert "exited 2" in reason


def test_triage_reads_a_real_verdict_unchanged(monkeypatch):
    monkeypatch.setattr(epic.shutil, "which", lambda _: "/usr/bin/claude")
    _fake_gh(monkeypatch, stdout=json.dumps(
        {"doable": True, "reason": "self-contained", "model": "sonnet"}),
        stderr="loaded 3 plugins")
    assert epic.triage(mk(1), "opus") == (True, "self-contained", "sonnet")
