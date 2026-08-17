"""Something has to run the flake checks (#179).

Workflow-shape assertions, in the manner of ``test_announce_workflow.py``, plus one
behavioural test. The property that broke is not textual: ``flake.nix`` declared four
checks and no workflow referenced the flake at all, so they ran when a human typed
``nix build`` and at no other time. That is how ``worktree-tests`` stayed red on ``main``
for a day with eight release-number assertions erroring inside it (#163).

The behavioural test is the one that matters most. ``nix flake check`` on a flake whose
checks have been removed exits 0 — so the obvious job, the one that only runs that
command, reports green the day the checks disappear. The discovery step exists to make
that loud, and a guard nobody exercises is the thing this repo keeps re-learning.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _flake_job() -> dict:
    """The job that runs the flake, found by what it does rather than by its name."""
    jobs = _workflow("tests.yml")["jobs"]
    running = [job for job in jobs.values()
               if any("nix flake check" in str(step.get("run", "")) for step in job["steps"])]
    assert running, (
        "no job in tests.yml runs `nix flake check`, so flake.nix's checks run only when "
        "somebody types `nix build` by hand — the state #179 was filed about")
    assert len(running) == 1, "two jobs run the flake; one of them is doing it twice over"
    return running[0]


def _discovery_script(job: dict) -> str:
    steps = [step for step in job["steps"] if "checks" in str(step.get("name", "")).lower()]
    assert steps, "the flake job has no step that names the checks before running them"
    return steps[0]["run"]


def test_a_job_runs_the_flake_checks():
    job = _flake_job()
    # Every job in this file carries one, and this is the slowest: it fetches a python
    # and tmux closure before a test runs.
    assert "timeout-minutes" in job


def test_the_flake_job_reports_on_pull_requests_too():
    """The `stamped` job below it is main-only by design and must NOT be a required
    check. This one is the opposite: a flake that only breaks after the merge is a
    consumer's problem to discover, which is the arrangement #179 replaced."""
    assert "if" not in _flake_job(), (
        "the flake job has become conditional — if it no longer runs on pull_request it "
        "cannot gate anything, and requiring it would hang every PR waiting for a run "
        "that by design never happens")


def _run_discovery(tmp_path: Path, names_json: str) -> subprocess.CompletedProcess:
    """Run the workflow's own discovery script against a stubbed `nix`.

    The stub is written with this interpreter's absolute path rather than
    ``/usr/bin/env``: a runtime stub whose shebang cannot be resolved is the failure
    that cost three suites a day between them (#177), and there is no reason to write
    a fourth instance even somewhere it would work.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    nix = stub_dir / "nix"
    nix.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('x86_64-linux' if 'currentSystem' in ' '.join(sys.argv) "
        f"else {names_json!r})\n"
    )
    nix.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run(["bash", "-c", _discovery_script(_flake_job())],
                          capture_output=True, text=True, env=env)


@pytest.mark.skipif(not shutil.which("jq") or not shutil.which("bash"),
                    reason="the discovery step is bash and jq; neither is worth vendoring")
def test_the_discovery_step_fails_loudly_when_the_flake_declares_no_checks(tmp_path):
    proc = _run_discovery(tmp_path, "[]")
    assert proc.returncode != 0, (
        "the flake declared no checks and the job carried on — which is exactly the green "
        "report #179 exists to prevent, since `nix flake check` itself exits 0 here")
    assert "::error::" in proc.stdout, "the failure has to be annotated, not just non-zero"


@pytest.mark.skipif(not shutil.which("jq") or not shutil.which("bash"),
                    reason="the discovery step is bash and jq; neither is worth vendoring")
def test_the_discovery_step_names_the_checks_it_found(tmp_path):
    proc = _run_discovery(tmp_path, '["worktree-tests","mcp-tests"]')
    assert proc.returncode == 0, proc.stderr
    # Named in the log, not merely counted: this is how a reviewer sees that a check
    # added by a PR is actually being run rather than assumed to be.
    assert "worktree-tests" in proc.stdout and "mcp-tests" in proc.stdout
    assert "2 flake check(s)" in proc.stdout
