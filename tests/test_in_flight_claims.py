"""`GET /claims/in-flight` — how much work is in flight in a repo (#337).

The number nothing in the fleet has ever known. Eight agents were run against one
`main` on 2026-08-22 and every predicted cost arrived: two branches minted
migration `0029` independently, a third was renumbered twice mid-flight, the
largest open diff went DIRTY the moment the first landed. Five mechanisms look
like they might govern that and every one is *downstream* of the decision to
start — the merge claim is the last few seconds of a branch's life, the merge
queue and `app/ordering.py` order an exit and an intent, the review queue
measures a backlog it does not bound.

**Claims are the count, and the reason is jurisdictional.** Not worktrees — 48 on
that box, mostly debris — and not open PRs, by which time the branch exists.
Quarterback bounds what it has authority over: an unlanded unit of work inside
this system is represented by a claim, and one outside it was never told to us.

So the properties under test are the ones that make a COUNT usable as a gate:

* it counts units of work in a REPOSITORY, and three kinds of live `work` claim
  are deliberately not that — a plan, an item, and #232's `plan-order:<repo>`;
* it counts across HOLDERS, because the bound is per repo and the fleet is
  several machines against one board. A per-holder count would admit `max` agents
  per box, and it is also what makes a human's checkout absorbed rather than
  exempt: they run `create-worktree` and take the same claim an agent does;
* a released or lapsed claim is gone from it, because a window that counts
  finished work throttles the fleet hardest right after it has been most
  productive — which is the failure that made #337's release half a precondition
  for its bound half.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from .conftest import DESKTOP, LAPTOP, SERVER


@pytest.fixture
def repo() -> str:
    """A repository name nothing else in this suite has claimed anything in.

    The schema is built once for the whole session, so claims outlive the test
    that took them — and a COUNT is the one assertion that cannot tolerate a
    neighbour's leftovers. Fixed keys are enough for `test_resource_claims.py`,
    which asserts about one key at a time; here the answer is a number over
    every key in a repo.
    """
    return f"acme/inflight-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def other_repo() -> str:
    """A second one, for the tests about the window being per repository."""
    return f"acme/elsewhere-{uuid.uuid4().hex[:10]}"


async def claim(client, headers=LAPTOP, **body):
    r = await client.post("/claim", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def count(client, repo, headers=LAPTOP) -> dict:
    r = await client.get("/claims/in-flight", params={"repo": repo}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def take_issue(client, repo, number: int, headers=LAPTOP, **over):
    return await claim(client, headers=headers,
                       ref={"kind": "issue", "repo": repo, "value": str(number)},
                       **over)


# ------------------------------------------------------------- what it counts

async def test_an_empty_board_is_zero_rather_than_absent(client, repo):
    """A gate reads `count`; an answer that omitted it for "nothing here" would
    make the caller invent a value on exactly the path it exists to bound."""
    answer = await count(client, repo)
    assert answer["count"] == 0 and answer["claims"] == []
    assert answer["repo"] == repo


async def test_each_claimed_issue_is_one_unit_in_flight(client, repo):
    for n in (1, 2, 3):
        await take_issue(client, repo, n, note=f"worktree feat/issue-{n}")
    assert (await count(client, repo))["count"] == 3


async def test_a_claimed_PR_counts_too(client, repo):
    """A PR in review is unlanded work that holds integration risk — the issue's
    own observation about the states diverging."""
    await take_issue(client, repo, 1)
    await claim(client, ref={"kind": "pr", "repo": repo, "value": "9"})
    assert (await count(client, repo))["count"] == 2


async def test_another_repos_work_is_not_in_this_window(client, repo, other_repo):
    """The bound is per repository. The repo is derived from the KEY, server-side,
    which is the join #172 exists to have exactly one implementation of."""
    await take_issue(client, repo, 1)
    await take_issue(client, other_repo, 2)
    assert (await count(client, repo))["count"] == 1
    assert (await count(client, other_repo))["count"] == 1


async def test_the_repo_is_canonicalised_before_it_is_matched(client, repo):
    """A caller that asked about `Acme/Widget` and was told nothing was in flight
    would be the #172 defect with the parties swapped."""
    await take_issue(client, repo, 1)
    assert (await count(client, repo.upper()))["count"] == 1


async def test_a_repo_the_board_cannot_key_is_refused_rather_than_answered_zero(client):
    r = await client.get("/claims/in-flight", params={"repo": "not-a-repo"},
                         headers=LAPTOP)
    assert r.status_code == 422


# --------------------------------------------------- four things that are not work

async def test_a_plan_claim_is_not_a_unit_of_work_in_flight(client, repo):
    """A plan is a board object. The planner holding the ordering is not a ninth
    agent in flight, and counting it would spend a slot on the thing that decides
    which slot to spend."""
    await claim(client, ref={"kind": "plan", "value": str(uuid.uuid4())})
    assert (await count(client, repo))["count"] == 0


async def test_an_item_claim_is_not_one_either(client, repo):
    await claim(client, ref={"kind": "item", "value": str(uuid.uuid4())})
    assert (await count(client, repo))["count"] == 0


async def test_232s_plan_order_claim_is_not_one(client, repo):
    """`kind=work, key=plan-order:<repo>` — the ordering claim the planner holds
    while it decides. It drops out because `repo_of` answers None for it, not
    because of a name match here; a special case for one spelling is how the next
    one gets counted."""
    await claim(client, kind="work", key=f"plan-order:{repo}")
    assert (await count(client, repo))["count"] == 0


async def test_a_merge_claim_is_not_one(client, repo):
    """#99's slot: somebody is landing on this branch right now. By definition
    work that is already built, and the last few seconds of its life."""
    await claim(client, kind="merge", key=f"{repo}:main")
    assert (await count(client, repo))["count"] == 0


async def test_a_legacy_kind_spelled_issue_still_counts(client, repo):
    """Rows written before #172 stored `kind='issue'` against the same key and the
    same unique index. Counting only the canonical spelling would under-report the
    window and admit an agent into a slot that is taken."""
    await claim(client, kind="issue", key=f"{repo}#4")
    answer = await count(client, repo)
    assert answer["count"] == 1
    assert answer["claims"][0]["kind"] == "work", (
        "the write path canonicalises; the count must read what was written")


# ------------------------------------------------------------- across the fleet

async def test_it_counts_every_holder_not_just_the_caller(client, repo):
    """The bound is per repo and the fleet is several machines against one board.
    A per-holder count would admit `max` agents PER BOX, which is not a bound on
    anything anybody cares about. It is also what absorbs a human's checkout
    rather than exempting it: they run `create-worktree` and take the same claim
    an agent does, and the count neither knows nor cares which it was."""
    await take_issue(client, repo, 1, headers=LAPTOP)
    await take_issue(client, repo, 2, headers=SERVER)
    await take_issue(client, repo, 3, headers=DESKTOP)
    answer = await count(client, repo, headers=LAPTOP)
    assert answer["count"] == 3
    assert answer["holders"] == ["desktop", "laptop", "server"]


async def test_a_claim_with_no_session_counts_like_any_other(client, repo):
    """The checkout claim names no session — the agent that will use the tree does
    not exist when it is taken. It is the commonest thing in the window."""
    await take_issue(client, repo, 1, note="worktree feat/issue-1 on zeus")
    assert (await count(client, repo))["count"] == 1


# ------------------------------------------------------ finished work is not in it

async def test_a_released_claim_leaves_the_window(client, repo):
    """The half of #337 that had to be built first. Without it the count is
    highest immediately after the fleet has been most productive, and a WIP limit
    that behaves that way gets switched off in a week."""
    taken = await take_issue(client, repo, 1)
    await take_issue(client, repo, 2)
    assert (await count(client, repo))["count"] == 2

    r = await client.post("/claim/release", json={"claim_id": taken["claim_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert (await count(client, repo))["count"] == 1


async def test_a_lapsed_claim_is_not_in_the_window(client, repo):
    """The self-healing property that makes claims the right count: a session that
    dies frees its slot with nobody intervening (#135). Read the way `GET /claims`
    reads it — filtered on the way past rather than swept, because a read must not
    mutate and the agent deciding whether to wait needs the truthful answer."""
    await take_issue(client, repo, 1, ttl=1)
    await take_issue(client, repo, 2)
    await asyncio.sleep(1.1)
    answer = await count(client, repo)
    assert answer["count"] == 1
    assert answer["claims"][0]["key"] == f"{repo}#2"


# --------------------------------------------------------------------- the answer

async def test_the_claims_carry_who_and_why_so_a_refusal_can_name_them(client, repo):
    """A window that refuses without saying who has the slots sends the reader to
    a dashboard to find out what refused them — the state `qb-claim`'s hold
    already avoids by naming its holder and their note."""
    await take_issue(client, repo, 7, note="worktree feat/issue-7 on zeus")
    one = (await count(client, repo))["claims"][0]
    assert one["key"] == f"{repo}#7"
    assert one["holder"] == "laptop"
    assert one["note"] == "worktree feat/issue-7 on zeus"
    assert one["expires"]


async def test_reading_the_window_needs_a_token(client, repo):
    r = await client.get("/claims/in-flight", params={"repo": repo})
    assert r.status_code == 401
