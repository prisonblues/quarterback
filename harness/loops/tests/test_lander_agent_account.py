"""What the red-CI fixer says about a run that changed nothing.

`fix_red` opens a worktree on a red Dependabot branch, lets an edit-only agent
loose, and pushes if anything changed. Its whole account of a run that changed
nothing used to be "agent made no edits — nothing to push", which is an
observation with two opposite explanations: there was nothing to fix, or the
agent was stopped from fixing anything (a tool auto-denied, headless mode unable
to prompt). It runs from a timer, so the agent's own explanation was not merely
unread — it went nowhere. These pin that it is now kept and printed, and that a
run which did not happen is never followed by a push.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lander  # noqa: E402


def decision(number=7):
    return lander.Decision(number=number, title="Bump urllib3 from 2.4.0 to 2.5.0",
                           base="main", head="dependabot/pip/urllib3-2.5.0",
                           klass="patch_minor", checks="red", action="would-fix-red")


def stub_git(calls, staged_changes: bool):
    """Every git/gh call the fixer makes, succeeding; `git diff --cached --quiet`
    answers whether the agent left anything staged."""
    def run(cmd, **kwargs):
        calls.append(cmd)
        if "diff" in cmd and "--cached" in cmd:
            return subprocess.CompletedProcess(cmd, 1 if staged_changes else 0)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def arrange(monkeypatch, tmp_path, agent, staged_changes=False):
    """Wire fix_red up to a fake agent and a fake git. Returns the git call log."""
    calls = []
    monkeypatch.setattr(lander, "head_commit_author", lambda repo, branch: "dependabot[bot]")
    monkeypatch.setattr(subprocess, "run", stub_git(calls, staged_changes))
    monkeypatch.setattr(lander, "run_agent", lambda args, cwd=None: agent)
    lander.fix_red(decision(), "acme/thing", str(tmp_path / "repo"), execute=True)
    return calls


def pushed(calls) -> bool:
    return any("push" in cmd for cmd in calls)


def test_no_edits_quotes_what_the_agent_said(monkeypatch, tmp_path, capsys):
    """The disambiguating sentence: 'made no edits' plus the agent's last word."""
    agent = subprocess.CompletedProcess(
        [], 0, "I could not read the lockfile — the Read tool was denied.", "")
    arrange(monkeypatch, tmp_path, agent)

    out = capsys.readouterr().out
    assert "agent made no edits — nothing to push." in out
    assert "It said: I could not read the lockfile — the Read tool was denied." in out


def test_an_agent_that_exited_zero_saying_nothing_is_a_failure_not_a_no_op(
        monkeypatch, tmp_path, capsys):
    """The #19 shape at an uncaptured call site: exit 0, empty stdout, and the
    stderr that explains it. Reported as a failure, and nothing is pushed."""
    agent = subprocess.CompletedProcess([], 0, "", "API Error: Credit balance is too low")
    calls = arrange(monkeypatch, tmp_path, agent, staged_changes=True)

    out = capsys.readouterr().out
    assert "agent FAILED" in out
    assert "exited 0 having printed nothing" in out
    assert "Credit balance is too low" in out
    assert not pushed(calls)


def test_a_nonzero_exit_reports_the_reason_and_leaves_the_branch_alone(
        monkeypatch, tmp_path, capsys):
    agent = subprocess.CompletedProcess([], 2, "", "Error: unknown option --permission-mode")
    calls = arrange(monkeypatch, tmp_path, agent)

    out = capsys.readouterr().out
    assert "agent FAILED (exited 2 (Error: unknown option --permission-mode))" in out
    assert not pushed(calls)


def test_a_failed_agent_does_not_abort_the_rest_of_the_sweep(monkeypatch, tmp_path):
    """It used to raise out of fix_red (check=True), which abandoned every other
    dependabot PR in the run over one bad agent."""
    agent = subprocess.CompletedProcess([], 1, "", "boom")
    arrange(monkeypatch, tmp_path, agent)  # no exception


def test_real_edits_are_still_committed_and_pushed(monkeypatch, tmp_path, capsys):
    """The happy path is untouched: captured output changes what is reported, not
    what is done."""
    agent = subprocess.CompletedProcess([], 0, "Updated the pinned version.", "")
    calls = arrange(monkeypatch, tmp_path, agent, staged_changes=True)

    assert "committing + pushing fix" in capsys.readouterr().out
    assert pushed(calls)


def test_a_silent_agent_that_left_a_real_fix_still_gets_it_pushed(
        monkeypatch, tmp_path, capsys):
    """The other half of the exit-0-blank shape, and the regression that came
    with reading it as a failure.

    `agent_failure` fires on exit 0 with empty stdout, and returning there tore
    the worktree down in the `finally` — so a `claude -p` that edited the
    lockfile correctly and simply printed nothing (an output-format quirk, a
    suppressed final message, a hook redirecting stdout) had its fix deleted and
    the PR stayed red. The old `check=True` path staged, found the edits and
    pushed them.

    stderr is what separates this from the failing case above: there it named a
    cause and the staged work is half a fix; here nothing was said anywhere, so
    the staged diff is the only evidence there is, and CI is the gate."""
    agent = subprocess.CompletedProcess([], 0, "", "")
    calls = arrange(monkeypatch, tmp_path, agent, staged_changes=True)

    out = capsys.readouterr().out
    assert "agent FAILED" not in out
    assert "pushing them" in out
    assert any("push" in c for c in calls), "the fix reaches the branch"
