"""Promotion-gate state on the board — #852.

A gate counts clean heavy runs on one head against an exit bar. These pin the three
things that make the count trustworthy: a run counts only against the head it ran
on, a failed run zeroes it, and a publish that lands a different commit on the
gated branch moves the gate to that commit with nothing banked.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import delete, select, text

from app.api import gates as gates_api
from app.db import async_session
from app.models.gate import Gate
from app.models.post import Post

from .conftest import LAPTOP, SERVER

REPO = "prisonblues/lexray"
BRANCH = "integration/v22"
HEAD = "a" * 40
NEXT = "b" * 40


#: Every branch a test here publishes on. Their posts are this file's alone.
BRANCHES = (BRANCH, "integration/v23", "feat/x")


@pytest.fixture(autouse=True)
async def _no_gates(client):
    """The schema is built once per session, and a commit an earlier test
    published counts as already published (`_published_before`), so every test
    starts with no gates and none of this file's posts."""
    async with async_session() as s:
        await s.execute(delete(Gate))
        for b in BRANCHES:
            await s.execute(delete(Post).where(
                Post.refs.contains([{"kind": "branch", "value": b}])))
        await s.commit()


async def start(client, head=HEAD, headers=LAPTOP, **extra):
    body = {"repo": REPO, "branch": BRANCH, "head": head, **extra}
    return await client.post("/gates", json=body, headers=headers)


async def advance(client, result, head=HEAD, **extra):
    body = {"repo": REPO, "branch": BRANCH, "head": head, "result": result, **extra}
    return await client.post("/gates/advance", json=body, headers=LAPTOP)


async def the_gate(client) -> dict:
    r = await client.get("/gates", params={"repo": REPO, "branch": BRANCH},
                         headers=SERVER)
    assert r.status_code == 200, r.text
    gates = r.json()["gates"]
    assert len(gates) == 1, gates
    return gates[0]


async def publish(client, commit, branch=BRANCH, repo=REPO, type_="published"):
    refs = [{"kind": "commit", "value": commit, "repo": repo},
            {"kind": "repo", "value": repo.split("/")[-1]},
            {"kind": "branch", "value": branch}]
    r = await client.post("/post", json={"type": type_, "summary": "Merge pull request #9",
                                         "refs": refs}, headers=SERVER)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_a_new_gate_is_readable_by_any_agent_with_nothing_banked(client):
    r = await start(client, exit_bar=2, stage="fast", outputs="zeus:~/sim-runs/v22")
    assert r.status_code == 200, r.text
    assert r.json()["previous"] is None
    g = await the_gate(client)
    assert g | {"head": HEAD, "clean_runs": 0, "exit_bar": 2, "passed": False,
                "stage": "fast", "outputs": "zeus:~/sim-runs/v22",
                "updated_by": "laptop"} == g


async def test_two_clean_runs_on_one_head_pass_a_bar_of_two(client):
    await start(client, exit_bar=2)
    await advance(client, "clean", stage="sim run 1")
    assert (await the_gate(client))["passed"] is False
    r = await advance(client, "clean", stage="sim run 2")
    assert r.status_code == 200, r.text
    g = r.json()["gate"]
    assert (g["clean_runs"], g["passed"], g["stage"], g["last_result"]) == (
        2, True, "sim run 2", "clean")


async def test_a_failed_run_zeroes_the_count(client):
    """The bar is CONSECUTIVE clean runs: a failure in between starts it again."""
    await start(client)
    await advance(client, "clean")
    r = await advance(client, "failed")
    assert r.json()["gate"]["clean_runs"] == 0
    assert r.json()["gate"]["last_result"] == "failed"


async def test_a_run_on_a_head_the_gate_is_not_at_is_refused_and_not_counted(client):
    """The superseded-run case from lexray v22: a run that finishes after the
    branch moved must not be credited to the new head."""
    await start(client)
    r = await advance(client, "clean", head=NEXT)
    assert r.status_code == 409
    assert r.json()["detail"]["gate_head"] == HEAD
    assert (await the_gate(client))["clean_runs"] == 0


async def test_setting_a_new_head_zeroes_the_count_and_says_where_it_was(client):
    await start(client, stage="sim run 2")
    await advance(client, "clean")
    r = await start(client, head=NEXT)
    assert r.json()["previous"]["clean_runs"] == 1
    g = r.json()["gate"]
    assert (g["head"], g["clean_runs"], g["stage"], g["restarted_from"]) == (
        NEXT, 0, None, HEAD)


async def test_setting_the_same_head_again_keeps_the_count(client):
    """Updating the stage or the outputs is not a new head."""
    await start(client)
    await advance(client, "clean")
    r = await start(client, outputs="zeus:~/sim-runs/v22b")
    assert r.json()["gate"]["clean_runs"] == 1
    assert r.json()["gate"]["outputs"] == "zeus:~/sim-runs/v22b"


async def test_a_publish_on_the_gated_branch_restarts_the_gate_and_says_so(client):
    await start(client, exit_bar=2)
    await advance(client, "clean")
    pid = await publish(client, NEXT)
    g = await the_gate(client)
    assert (g["head"], g["clean_runs"], g["restarted_from"]) == (NEXT, 0, HEAD)
    async with async_session() as s:
        said = (await s.execute(select(Post).where(Post.re == pid))).scalars().all()
    assert len(said) == 1
    assert said[0].type == "status"
    assert f"restarts at {NEXT[:12]}" in said[0].summary
    assert "1/2 clean" in said[0].summary
    assert said[0].author == "server"


async def test_a_run_finishing_after_a_publish_is_refused(client):
    await start(client)
    await publish(client, NEXT)
    r = await advance(client, "clean", head=HEAD)
    assert r.status_code == 409
    assert r.json()["detail"]["restarted_from"] == HEAD


async def answers_to(pid: int) -> list[Post]:
    async with async_session() as s:
        return list((await s.execute(select(Post).where(Post.re == pid))).scalars())


async def test_a_landed_post_does_not_move_the_gate(client):
    """`landed` says a commit exists somewhere, not that the runners can fetch it."""
    await start(client)
    pid = await publish(client, NEXT, type_="landed")
    assert (await the_gate(client))["head"] == HEAD
    assert await answers_to(pid) == []


async def test_a_reannounced_older_commit_does_not_move_the_gate(client):
    """A checkout behind its upstream, or CI re-announcing, publishes a commit the
    branch was already published at. That is not the branch moving."""
    await publish(client, HEAD)          # the branch was at HEAD…
    await publish(client, NEXT)          # …then moved to NEXT
    await start(client, head=NEXT)
    await advance(client, "clean", head=NEXT)
    pid = await publish(client, HEAD)    # a stale announcement of the old tip
    g = await the_gate(client)
    assert (g["head"], g["clean_runs"]) == (NEXT, 1)
    assert await answers_to(pid) == []


async def test_a_commit_published_on_another_branch_first_still_moves_the_gate(client):
    """A fast-forward merge brings a feature branch's commit to the gated branch:
    new HERE, whatever other branch it was published on."""
    await publish(client, NEXT, branch="feat/x")
    await start(client)
    await publish(client, NEXT)
    assert (await the_gate(client))["head"] == NEXT


async def test_an_abbreviated_publish_on_a_gated_branch_says_the_gate_did_not_move(client):
    await start(client)
    await advance(client, "clean")
    pid = await publish(client, NEXT[:7])
    g = await the_gate(client)
    assert (g["head"], g["clean_runs"]) == (HEAD, 1)
    said = await answers_to(pid)
    assert len(said) == 1 and "NOT moved" in said[0].summary


async def test_a_publish_naming_two_qualified_repos_moves_nothing(client):
    """Which repo's branch moved cannot be told, so nothing moves."""
    await start(client)
    refs = [{"kind": "commit", "value": NEXT, "repo": REPO},
            {"kind": "repo", "value": "prisonblues/quarterback"},
            {"kind": "branch", "value": BRANCH}]
    r = await client.post("/post", json={"type": "published", "summary": "x",
                                         "refs": refs}, headers=SERVER)
    assert r.status_code == 200
    assert (await the_gate(client))["head"] == HEAD


async def test_a_publish_with_no_refs_at_all_is_still_a_publish(client):
    await start(client)
    r = await client.post("/post", json={"type": "published", "summary": "x"},
                          headers=SERVER)
    assert r.status_code == 200
    assert (await the_gate(client))["head"] == HEAD


@pytest.mark.parametrize("boom", ["database", "python"])
async def test_a_failing_reset_costs_the_publish_nothing(client, monkeypatch, caplog,
                                                         boom):
    """The hook's promise: the publish commits, the gate is untouched, and the
    failure is logged. `database` raises a real Postgres error inside the savepoint
    (division by zero), which is what proves the savepoint leaves the outer
    transaction usable; `python` is any other defect in the hook."""
    await start(client)
    await advance(client, "clean")

    async def broken(session, *args):
        if boom == "database":
            await session.execute(text("SELECT 1/0"))
        raise RuntimeError("a defect in the hook")

    monkeypatch.setattr(gates_api, "_published_before", broken)
    with caplog.at_level(logging.ERROR, logger="app.api.gates"):
        pid = await publish(client, NEXT)
    async with async_session() as s:
        assert await s.get(Post, pid) is not None
    g = await the_gate(client)
    assert (g["head"], g["clean_runs"]) == (HEAD, 1)
    assert await answers_to(pid) == []
    assert "the post stands" in caplog.text


async def test_republishing_the_gate_s_own_head_changes_nothing(client):
    """CI announces the same commit a local push already did."""
    await start(client)
    await advance(client, "clean")
    pid = await publish(client, HEAD)
    assert (await the_gate(client))["clean_runs"] == 1
    async with async_session() as s:
        assert not (await s.execute(select(Post).where(Post.re == pid))).scalars().all()


@pytest.mark.parametrize("branch,repo", [
    ("integration/v23", REPO),          # another branch
    (BRANCH, "prisonblues/quarterback"),  # another repo, same branch name
])
async def test_a_publish_elsewhere_leaves_the_gate_alone(client, branch, repo):
    await start(client)
    await advance(client, "clean")
    await publish(client, NEXT, branch=branch, repo=repo)
    g = await the_gate(client)
    assert (g["head"], g["clean_runs"]) == (HEAD, 1)


async def test_a_publish_naming_no_qualified_repo_moves_nothing(client):
    """A bare repo name is the ambiguity `app.landing.post_repos` refuses."""
    await start(client)
    refs = [{"kind": "commit", "value": NEXT}, {"kind": "repo", "value": "lexray"},
            {"kind": "branch", "value": BRANCH}]
    r = await client.post("/post", json={"type": "published", "summary": "x",
                                         "refs": refs}, headers=SERVER)
    assert r.status_code == 200
    assert (await the_gate(client))["head"] == HEAD


async def test_setting_a_head_behind_the_branch_says_so(client):
    """A gate set on a commit the branch has already been published past would
    never be moved by a publish that came before it."""
    await publish(client, NEXT)
    r = await start(client, head=HEAD)
    assert (r.json()["branch_published"], r.json()["behind"]) == (NEXT, True)
    r = await start(client, head=NEXT)
    assert r.json()["behind"] is False


async def test_the_reset_rule_round_trips_and_omitting_it_keeps_it(client):
    await start(client, reset_rule="a change to sim/ data")
    r = await start(client, stage="db")
    assert r.json()["gate"]["reset_rule"] == "a change to sim/ data"


async def test_reading_with_no_filter_lists_every_repo_s_gates(client):
    await start(client)
    await client.post("/gates", json={"repo": "prisonblues/quarterback",
                                      "branch": "main", "head": NEXT}, headers=LAPTOP)
    r = await client.get("/gates", headers=SERVER)
    assert [(g["repo"], g["branch"]) for g in r.json()["gates"]] == [
        (REPO, BRANCH), ("prisonblues/quarterback", "main")]


async def test_a_run_recorded_while_another_write_holds_the_gate_is_not_lost(client):
    """`_locked`'s FOR UPDATE. A second writer holds the row and moves the count to
    5; an advance arriving meanwhile must wait and read 5. Without the lock it
    reads 0 before the other commits and writes 1 over the 5."""
    await start(client, exit_bar=9)
    async with async_session() as other:
        g = await gates_api._locked(other, REPO, BRANCH)
        pending = asyncio.ensure_future(advance(client, "clean"))
        await asyncio.sleep(0.3)
        g.clean_runs = 5
        await other.commit()
    r = await asyncio.wait_for(pending, 10)
    assert r.status_code == 200, r.text
    assert (await the_gate(client))["clean_runs"] == 6


async def test_two_gates_started_at_once_give_the_loser_a_409(client, monkeypatch):
    """The unique index is the backstop when both writers saw no row."""
    await start(client)

    async def nothing(*args):
        return None

    monkeypatch.setattr(gates_api, "_locked", nothing)
    r = await start(client, head=NEXT)
    assert r.status_code == 409


async def test_an_abbreviated_head_is_refused(client):
    r = await start(client, head="abc1234")
    assert r.status_code == 422


async def test_advancing_a_branch_with_no_gate_is_a_404(client):
    r = await advance(client, "clean")
    assert r.status_code == 404


async def test_the_repo_is_folded_and_the_branch_is_not(client):
    r = await client.post("/gates", json={"repo": "PrisonBlues/LexRay",
                                          "branch": BRANCH, "head": HEAD},
                          headers=LAPTOP)
    assert r.json()["gate"]["repo"] == REPO
    r = await client.get("/gates", params={"repo": REPO, "branch": BRANCH.upper()},
                         headers=SERVER)
    assert r.json()["gates"] == []


async def test_a_branch_without_its_repo_is_refused(client):
    r = await client.get("/gates", params={"branch": BRANCH}, headers=SERVER)
    assert r.status_code == 422


async def test_clearing_removes_it_and_clearing_again_is_not_an_error(client):
    await start(client)
    r = await client.post("/gates/clear", json={"repo": REPO, "branch": BRANCH},
                          headers=LAPTOP)
    assert r.json()["cleared"]["head"] == HEAD
    r = await client.post("/gates/clear", json={"repo": REPO, "branch": BRANCH},
                          headers=LAPTOP)
    assert r.status_code == 200 and r.json()["cleared"] is None
    r = await client.get("/gates", params={"repo": REPO}, headers=SERVER)
    assert r.json()["gates"] == []
