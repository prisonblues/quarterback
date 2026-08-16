"""v2.34: the origin-moved signal stops answering "in sync" when it didn't look.

Two halves of one blindness (#125, #127). The board's staleness verdict is a
comparison against the ``published`` line, so a repo with nothing on that line
gets ``stale: false`` — which reads as "you're current" and means "we have
nothing to compare against". Those are different answers and callers need them
apart.

The workflow tests are here rather than in a lint because the property that
broke is structural, not textual: the announce used to be a step inside the
``deploy`` job, which needs ``build-and-push``, so a red image build silently
swallowed the announcement that main had moved. Asserting "no needs:" is the
only thing that stops that being reintroduced by someone tidying the file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .conftest import LAPTOP, SERVER

REPO = "v234repo"

B = "bbbbbbb2222222222222222222222222222222222"
C = "ccccccc3333333333333333333333333333333333"

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


async def _publish(client, repo: str, sha: str, summary: str, branch: str = "main"):
    return await client.post(
        "/post",
        json={
            "type": "published",
            "summary": summary,
            "refs": [
                {"kind": "repo", "value": repo},
                {"kind": "branch", "value": branch},
                {"kind": "commit", "value": sha},
            ],
        },
        headers=SERVER,
    )


# ---- #125: "in sync" vs "nothing to compare against" -------------------------


async def test_a_repo_with_no_published_line_is_not_comparable(client):
    # nix-fleet's shape: no CI, so nothing ever announces, so the board holds no
    # published line for it at all. The verdict must not read as a clean bill.
    res = (
        await client.get(
            "/sync",
            params={"repo": f"{REPO}-silent", "branch": "main", "have": f"{C},{B}"},
            headers=LAPTOP,
        )
    ).json()
    assert res["comparable"] is False
    assert res["published"] == []
    assert res["stale"] is False  # we genuinely don't know — don't claim staleness


async def test_one_publish_is_enough_to_be_comparable(client):
    repo = f"{REPO}-comparable"
    await _publish(client, repo, C, "shipped")
    res = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "have": f"{C},{B}"}, headers=LAPTOP
        )
    ).json()
    assert res["comparable"] is True
    assert res["stale"] is False and res["advice"] is None  # current, and told so by silence


async def test_no_published_line_and_no_upstream_breaks_silence(client):
    # Both sources absent: nothing published, and the caller sends no `behind`
    # because it has no upstream to count against. Silence here would be the one
    # answer we can't support.
    res = (
        await client.get(
            "/sync",
            params={"repo": f"{REPO}-blind", "branch": "main", "have": f"{C},{B}"},
            headers=LAPTOP,
        )
    ).json()
    assert res["comparable"] is False
    assert res["advice"] is not None
    assert "unknown" in res["advice"]


async def test_a_local_upstream_count_keeps_the_advisory_quiet(client):
    # Deliberately narrow: with `behind` present we still hold the weak local
    # signal, so we stay quiet rather than nag a CI-less repo every session.
    res = (
        await client.get(
            "/sync",
            params={
                "repo": f"{REPO}-quiet",
                "branch": "main",
                "have": f"{C},{B}",
                "behind": "0",
            },
            headers=LAPTOP,
        )
    ).json()
    assert res["comparable"] is False  # still can't compare...
    assert res["advice"] is None  # ...but the local ref is a signal, so no nag


async def test_the_weak_local_signal_still_reports_staleness(client):
    # `behind` can only under-report (a stale @{u} ref counts too few commits,
    # never too many), so a non-zero count is a true positive even unfetched.
    res = (
        await client.get(
            "/sync",
            params={
                "repo": f"{REPO}-behind",
                "branch": "main",
                "have": f"{C},{B}",
                "behind": "3",
            },
            headers=LAPTOP,
        )
    ).json()
    assert res["stale"] is True
    assert "3 commits behind" in res["advice"]


async def test_comparable_is_judged_on_the_branch_line_asked_about(client):
    # A publish onto main says nothing about whether a feature line is comparable.
    repo = f"{REPO}-branchline"
    await _publish(client, repo, C, "shipped on main", branch="main")
    feat = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "feat/x", "have": B}, headers=LAPTOP
        )
    ).json()
    assert feat["comparable"] is False
    main = (
        await client.get(
            "/sync", params={"repo": repo, "branch": "main", "have": B}, headers=LAPTOP
        )
    ).json()
    assert main["comparable"] is True


async def test_an_unregistered_scoped_worktree_keeps_its_own_answer(client):
    # The pre-existing fallback must win: "not registered" is more actionable
    # than "not comparable", and both can be true at once.
    repo = f"{REPO}-scoped"
    res = (
        await client.get("/sync", params={"repo": repo, "device": "never-reported"}, headers=LAPTOP)
    ).json()
    assert res["comparable"] is False
    assert "report_git" in res["advice"]


# ---- #127: the announce must not ride on the image build --------------------


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
