"""The announce must not ride on the image build (#127).

These are workflow-shape assertions rather than a lint because the property
that broke is structural, not textual: the announce used to be a step inside
the ``deploy`` job, which needs ``build-and-push``, so a red image build
silently swallowed the announcement that main had moved. Asserting "no needs:"
is the only thing that stops that being reintroduced by someone tidying the
file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def test_the_announce_job_does_not_depend_on_the_build():
    # b86ff0b (merge of #134) is an ancestor of main with no `published` post on
    # the board: its build went red, so the deploy job never ran, so the step
    # that announces main had moved never ran either.
    jobs = _workflow("docker-build.yml")["jobs"]
    assert "announce" in jobs
    assert "needs" not in jobs["announce"]
    assert jobs["announce"]["uses"].endswith("announce-board.yml")


def test_the_deploy_job_no_longer_announces():
    deploy = _workflow("docker-build.yml")["jobs"]["deploy"]
    assert deploy["needs"] == "build-and-push"  # deploying *is* contingent on a build
    # The announce must not have been left behind here as well as moved: two
    # sources would double-post every commit onto the board.
    assert not any("QUARTERBACK_TOKEN" in str(step) for step in deploy["steps"])


def test_the_announce_is_reusable_by_other_repos():
    # nix-fleet has no CI at all and lexray announces nothing; enrolment has to be
    # cheaper than a copy-paste or it won't happen (and for a year, didn't).
    wf = _workflow("announce-board.yml")
    # PyYAML resolves a bare `on:` key to the boolean True — YAML 1.1's fault.
    trigger = wf.get("on", wf.get(True))
    assert "workflow_call" in trigger
    assert set(trigger["workflow_call"]["secrets"]) == {
        "QUARTERBACK_TOKEN",
        "QUARTERBACK_BASE_URL",
    }


def test_a_failed_announce_is_visible_rather_than_swallowed():
    # The old form was `curl … && echo ok || echo failed`, which always exited 0:
    # a rotated token would stop every announcement with nothing saying so.
    step = _workflow("announce-board.yml")["jobs"]["announce"]["steps"][0]
    assert step["continue-on-error"] is True  # never fails someone's merge...
    assert "|| echo" not in step["run"]  # ...but the step still goes red
    assert "set -euo pipefail" in step["run"]
