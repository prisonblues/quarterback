"""v2.39: the plan — what is next, in what order, and who has it.

The board could say who was here and what they had just published. It could not
say what to do next, so every agent guessed: three of them once fixed the same
red CI job in one morning, and the third had checked for peers first and been
told the coast was clear. Presence answered "is anyone in this file"; nothing
answered "is this already taken".

So the properties under test are the ones that make an ordered list of items a
coordination primitive rather than a to-do list in a table:

* **A cold agent gets an ordered, unclaimed answer** — `next` is worked out by
  the board, not inferred by the caller from a list it has to interpret.
* **Claiming is visible, atomic, and expires by itself.** It is the SAME claim
  ``POST /claim`` writes, so the plan and the claims table cannot disagree, and
  a claim taken by hand on the work key shows in the plan unmediated.
* **A dead agent's claim disappears with nobody intervening** — the whole reason
  this is a lease and not a GitHub assignee.
* **The plan never duplicates an issue**, enforced by the database rather than
  by everyone remembering: one open item per ref.
* **"Not yet" is a fact.** A dependency blocks, a dropped one does not block
  forever, and a circular one is refused.
* **Only a human reorders — and placing a new item is not reordering (#183).**
  Permuting existing items is contested, so the sequence stays the human's, which
  is what stops it thrashing. Saying where a NEW item enters alters no existing
  pair's relative order, so an agent may do it — and `next` says out loud how much
  of the order anybody actually chose, instead of answering rank 1 with confidence
  while the human's stated top priority sits at rank 20.
* **It never decides an item is done** — `done` records that the issue closed.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

from app.api.claims import ClaimRequest, acquire
from app.api.plan import CLAIM_KIND, STALE_DAYS, _item_view
from app.config import settings
from app.db import async_session
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease

from .conftest import DESKTOP, LAPTOP, PINNED_SETTINGS, SERVER

#: A person, as the edge proves it: the identity header AND the secret only the
#: proxy knows. `Remote-User` alone is what any caller can send, which is why it
#: is no longer enough on its own — see `app.auth.human`.
HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
#: What an agent can trivially forge, and must not be believed.
SPOOFED = {"Remote-User": "rich"}


async def add(client, repo: str, title: str, headers=LAPTOP, **over) -> dict:
    r = await client.post("/plan/item", json={"repo": repo, "title": title, **over},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def issue(client, repo: str, number: int, headers=LAPTOP, **over) -> dict:
    return await add(client, repo, f"#{number}", headers=headers,
                     ref_kind="issue", ref_value=str(number), **over)


async def read(client, repo: str | None = None, headers=LAPTOP, **params) -> dict:
    r = await client.get("/plan", params={**({"repo": repo} if repo else {}), **params},
                         headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def claim_item(client, item_id: str, headers=LAPTOP, **over):
    return await client.post("/plan/item/claim", json={"item_id": item_id, **over},
                             headers=headers)


async def take(client, item_id: str, headers=LAPTOP, **over) -> dict:
    r = await claim_item(client, item_id, headers=headers, **over)
    assert r.status_code == 200, r.text
    return r.json()


async def _expire(key: str) -> None:
    """Age a live claim out, instead of waiting for the wall clock to do it."""
    async with async_session() as s:
        await s.execute(
            update(ResourceLease)
            .where(ResourceLease.kind == CLAIM_KIND, ResourceLease.key == key,
                   ResourceLease.released_at.is_(None))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()


# ------------------------------------------- an ordered answer, not a guess

async def test_a_cold_agent_is_told_what_is_next_rather_than_working_it_out(client):
    """The one call an agent makes when it starts. `next` is the answer itself —
    an agent that reads nothing else still picks the right thing up."""
    repo = "acme/cold"
    first = await issue(client, repo, 55, note="before #53: its schema is what #53 queries")
    second = await issue(client, repo, 53)

    plan = await read(client, repo)
    assert [i["item_id"] for i in plan["items"]] == [first["item_id"], second["item_id"]]
    assert [i["rank"] for i in plan["items"]] == [1, 2]
    assert plan["next"]["item_id"] == first["item_id"]
    # The reasoning behind the order travels with it — that sentence is the half
    # a GitHub issue has no field for, and the reason the plan exists at all.
    assert plan["next"]["note"].startswith("before #53")
    # The live counts are this repo's; `done`/`dropped` are deliberately not
    # asserted here, because a repo read's scope includes the fleet-wide items
    # and therefore every fleet item any other test has ever finished.
    assert {k: plan["counts"][k] for k in ("open", "claimed", "blocked", "stale")} == {
        "open": 2, "claimed": 0, "blocked": 0, "stale": 0}
    assert plan["truncated"] is False


async def test_the_plan_links_to_an_issue_and_never_restates_it(client):
    """One open item per issue, refused by the database and not by convention —
    two rows about #60 is exactly the drift this table exists to remove."""
    repo = "acme/norestate"
    first = await issue(client, repo, 60)

    r = await client.post("/plan/item", json={
        "repo": repo, "title": "the plan thing again", "ref_kind": "issue",
        # Spelled differently, and still the same issue: normalised at the edge,
        # or the unique index would never see the collision.
        "ref_value": "#60"}, headers=DESKTOP)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["item_id"] == first["item_id"]


async def test_a_ref_is_both_halves_or_neither(client):
    """A kind with no value links nowhere; a value with no kind cannot be
    rendered or claimed against the convention agents already use."""
    r = await client.post("/plan/item", json={"repo": "acme/halfref", "title": "half",
                                              "ref_kind": "issue"}, headers=LAPTOP)
    assert r.status_code == 422


async def test_an_empty_repo_is_the_fleet_and_not_a_third_scope(client):
    """`repo=""` agreed with nothing: the unique index keys on
    `COALESCE(repo,'')` so it collided with the fleet items, `claim_key` read it
    as no repo, and ranking treated it as a scope of its own."""
    blank = await add(client, "", "blank-repo item", ref_kind="issue", ref_value="300")
    assert blank["repo"] is None

    dup = await client.post("/plan/item", json={"repo": None, "title": "same issue",
                                                "ref_kind": "issue", "ref_value": "300"},
                            headers=DESKTOP)
    assert dup.status_code == 409, "it is the same scope, so it is the same item"
    await client.post("/plan/item/done", json={"item_id": blank["item_id"]}, headers=LAPTOP)


async def test_adding_needs_a_token_and_reading_does_not(client):
    """Reading is a `reader` path so the human board can render it with no token;
    writing is an agent identity, because an item records who added it."""
    assert (await client.get("/plan", headers=HUMAN)).status_code == 200
    r = await client.post("/plan/item", json={"repo": "acme/noauth", "title": "x"},
                          headers=HUMAN)
    assert r.status_code == 401


# ------------------------------------------------------- claims, and expiry

async def test_claiming_an_item_is_visible_to_every_other_agent(client):
    """The claim is the post that prevents duplicated work. A `done` afterwards
    can only record it."""
    repo = "acme/claimvis"
    first = await issue(client, repo, 1, note="the one everyone would pick")
    second = await issue(client, repo, 2)
    taken = await take(client, first["item_id"], session="s-1", note="building it")
    assert taken["claim"]["holder"] == "laptop"

    plan = await read(client, repo, headers=DESKTOP)
    held = plan["items"][0]
    assert held["claim"]["holder"] == "laptop" and held["claim"]["note"] == "building it"
    # ...and the next agent is sent to the next free thing rather than to a wall.
    assert plan["next"]["item_id"] == second["item_id"]
    assert plan["counts"]["claimed"] == 1


async def test_a_second_claimant_is_refused_and_told_who_holds_it(client):
    """A refusal that names the holder and their session is somebody to talk to;
    one that says only "held" leaves the loser nothing to do but spin."""
    repo = "acme/contended"
    item = await issue(client, repo, 7)
    await take(client, item["item_id"], session="s-1", note="landing it")

    r = await claim_item(client, item["item_id"], headers=DESKTOP)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["held_by"] == "laptop" and detail["session"] == "s-1"
    assert detail["note"] == "landing it"


async def test_a_dead_agents_claim_disappears_without_anyone_intervening(client):
    """The acceptance criterion that rules out a GitHub assignee: an agent that
    dies at 3am must not hold an item until somebody notices. Nothing reaps it —
    the next claimant's own request sweeps the key.

    The clock is moved rather than waited on: a correctness assertion that
    depends on the scheduler having run within 100ms is a flake on a loaded box,
    and every other time-dependent property in this file is asserted the same
    way."""
    repo = "acme/deadagent"
    item = await issue(client, repo, 9)
    await take(client, item["item_id"], session="s-dies")
    await _expire(f"{repo}#9")

    plan = await read(client, repo, headers=DESKTOP)
    assert plan["items"][0]["claim"] is None
    assert plan["next"]["item_id"] == item["item_id"], "a lapsed claim must free the item"
    got = await take(client, item["item_id"], headers=DESKTOP)
    assert got["claim"]["holder"] == "desktop"


async def test_a_claim_taken_by_hand_shows_in_the_plan(client):
    """The reason there is no holder column. Agents were already claiming work as
    `kind='work'`, `key='<repo>#<issue>'` before this table existed; the plan
    reads those very rows, so the two views cannot drift apart."""
    repo = "acme/byhand"
    item = await issue(client, repo, 142)
    r = await client.post("/claim", json={"kind": "work", "key": f"{repo}#142",
                                          "session": "s-hand", "note": "claimed the old way"},
                          headers=SERVER)
    assert r.status_code == 200, r.text

    plan = await read(client, repo)
    assert plan["items"][0]["claim"]["holder"] == "server"
    assert plan["items"][0]["claim"]["note"] == "claimed the old way"
    assert plan["next"] is None
    assert item["claim"] is None, "it was free when it was added"


async def test_releasing_puts_it_back_and_is_idempotent(client):
    repo = "acme/release"
    item = await issue(client, repo, 3)
    await take(client, item["item_id"], session="s-1")

    r = await client.post("/plan/item/release",
                          json={"item_id": item["item_id"], "session": "s-1"},
                          headers=LAPTOP)
    assert r.status_code == 200 and r.json()["released"] is True
    assert r.json()["claim"] is None

    again = await client.post("/plan/item/release",
                              json={"item_id": item["item_id"], "session": "s-1"},
                              headers=LAPTOP)
    assert again.status_code == 200 and again.json()["released"] is False


async def test_only_the_holder_may_put_an_item_back(client):
    repo = "acme/notyours"
    item = await issue(client, repo, 4)
    await take(client, item["item_id"], session="s-1")

    r = await client.post("/plan/item/release", json={"item_id": item["item_id"]},
                          headers=DESKTOP)
    assert r.status_code == 403
    assert r.json()["detail"]["held_by"] == "laptop"


# ------------------------------------------- "not yet" as a fact, not a habit

async def test_a_blocked_item_is_never_next_and_is_refused_by_default(client):
    """#57's cost column is blocked on #15, and today that is expressible
    nowhere. An agent that takes it anyway has to say so."""
    repo = "acme/blocked"
    blocker = await issue(client, repo, 15)
    waiter = await issue(client, repo, 57, depends_on=["#15"])

    plan = await read(client, repo)
    waiting = next(i for i in plan["items"] if i["item_id"] == waiter["item_id"])
    assert waiting["blocked_by"][0]["item_id"] == blocker["item_id"]
    assert plan["next"]["item_id"] == blocker["item_id"]
    assert plan["counts"]["blocked"] == 1

    r = await claim_item(client, waiter["item_id"])
    assert r.status_code == 409 and r.json()["detail"]["blocked_by"]

    forced = await take(client, waiter["item_id"], force=True)
    assert forced["claim"]["holder"] == "laptop"


async def test_finishing_the_blocker_unblocks_what_waited_on_it(client):
    repo = "acme/unblock"
    blocker = await issue(client, repo, 15)
    waiter = await issue(client, repo, 57, depends_on=[blocker["item_id"]])

    r = await client.post("/plan/item/done", json={"item_id": blocker["item_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text

    plan = await read(client, repo)
    assert plan["next"]["item_id"] == waiter["item_id"]
    assert plan["items"][0]["blocked_by"] == []


async def test_a_dropped_dependency_does_not_block_forever(client):
    """Only OPEN dependencies block. A dropped one will never be done, and
    waiting on it would be the plan quietly lying about what is next."""
    repo = "acme/droppeddep"
    blocker = await issue(client, repo, 20)
    waiter = await issue(client, repo, 21, depends_on=["#20"])
    assert (await read(client, repo))["next"]["item_id"] == blocker["item_id"]

    r = await client.post("/plan/item/update",
                          json={"item_id": blocker["item_id"], "state": "dropped"},
                          headers=HUMAN)
    assert r.status_code == 200, r.text
    assert (await read(client, repo))["next"]["item_id"] == waiter["item_id"]


async def test_a_dependency_may_be_named_by_issue_number_or_by_item_id(client):
    """Agents and humans talk in issue numbers; the graph stores item ids, so a
    dependency never depends on a spelling."""
    repo = "acme/depspelling"
    blocker = await issue(client, repo, 30)
    waiter = await issue(client, repo, 31)

    r = await client.post("/plan/item/depends",
                          json={"item_id": waiter["item_id"], "depends_on": ["30"]},
                          headers=DESKTOP)
    assert r.status_code == 200, r.text
    assert r.json()["depends_on"] == [blocker["item_id"]]
    assert r.json()["blocked_by"][0]["ref"] == "30"


async def test_a_dependency_on_something_outside_the_plan_is_refused(client):
    """A dependency is a link between items, not a bare number: an item may not
    wait on something nothing is tracking."""
    repo = "acme/depmissing"
    item = await issue(client, repo, 40)
    r = await client.post("/plan/item/depends",
                          json={"item_id": item["item_id"], "depends_on": ["#999"]},
                          headers=LAPTOP)
    assert r.status_code == 422
    assert "references that issue" in str(r.json()["detail"])


async def test_a_fleet_items_dependency_does_not_bind_to_some_repos_issue(client):
    """A fleet item's "#15" is not "whichever repo happens to have a 15". Left
    unscoped it silently bound to an unrelated repo's item, so the plan blocked
    on work nobody meant."""
    await issue(client, "acme/elsewhere", 61)
    fleet = await add(client, None, "fleet-wide thing")

    r = await client.post("/plan/item/depends",
                          json={"item_id": fleet["item_id"], "depends_on": ["#61"]},
                          headers=LAPTOP)
    assert r.status_code == 422
    # Finish it: a fleet item is in EVERY repo's read by design, so leaving one
    # open here would quietly become the `next` of every test below it.
    await client.post("/plan/item/done", json={"item_id": fleet["item_id"]}, headers=LAPTOP)


async def test_a_plan_filter_is_applied_before_the_limit(client):
    """Filtering the returned page instead of the query dropped every match past
    the first `limit` rows — and with it `next`, which then read as "nothing to
    do in this plan" while the plan was full of work."""
    repo = "acme/planlimit"
    await issue(client, repo, 200, plan="stage 1")
    second = await issue(client, repo, 201, plan="stage 2")

    plan = await read(client, repo, plan="stage 2", limit=1)
    assert [i["item_id"] for i in plan["items"]] == [second["item_id"]]
    assert plan["next"]["item_id"] == second["item_id"]


async def test_reopening_an_item_whose_issue_was_retaken_is_refused_not_a_500(client):
    """The uniqueness rule holds on the way back in too — and the caller is told
    which item is in the way rather than being handed a server error."""
    repo = "acme/reopenclash"
    first = await issue(client, repo, 210)
    await client.post("/plan/item/update",
                      json={"item_id": first["item_id"], "state": "dropped"}, headers=HUMAN)
    second = await issue(client, repo, 210)

    r = await client.post("/plan/item/update",
                          json={"item_id": first["item_id"], "state": "open"}, headers=HUMAN)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["item_id"] == second["item_id"]


async def test_two_dependency_writes_cannot_both_close_the_same_cycle(client):
    """A race-based safeguard needs a concurrent test: v2.31 shipped one without.
    Both halves of A→B and B→A validate against a graph missing the other's edge
    unless the writes are serialised, and then both commit a cycle no single
    request was wrong to make."""
    repo = "acme/cyclerace"
    a = await issue(client, repo, 220)
    b = await issue(client, repo, 221)

    both = await asyncio.gather(
        client.post("/plan/item/depends",
                    json={"item_id": a["item_id"], "depends_on": [b["item_id"]]},
                    headers=LAPTOP),
        client.post("/plan/item/depends",
                    json={"item_id": b["item_id"], "depends_on": [a["item_id"]]},
                    headers=DESKTOP),
    )
    codes = sorted(r.status_code for r in both)
    assert codes == [200, 422], f"exactly one edge may land, got {codes}"


async def test_a_circular_dependency_is_refused(client):
    """Otherwise "what is unblocked?" has no answer, which is the one question
    the table is for."""
    repo = "acme/cycle"
    a = await issue(client, repo, 50)
    b = await issue(client, repo, 51, depends_on=["#50"])

    r = await client.post("/plan/item/depends",
                          json={"item_id": a["item_id"], "depends_on": [b["item_id"]]},
                          headers=LAPTOP)
    assert r.status_code == 422 and "circular" in str(r.json()["detail"])

    self_ref = await client.post("/plan/item/depends",
                                 json={"item_id": a["item_id"], "depends_on": [a["item_id"]]},
                                 headers=LAPTOP)
    assert self_ref.status_code == 422


# ------------------------------------------------- done records, never decides

async def test_done_releases_the_claim_and_records_who_and_when(client):
    repo = "acme/done"
    item = await issue(client, repo, 70)
    await take(client, item["item_id"], session="s-1")

    r = await client.post("/plan/item/done",
                          json={"item_id": item["item_id"], "session": "s-1",
                                "note": "landed in PR #143"},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "done" and body["done_by"] == "laptop" and body["done"]
    assert body["claim"] is None, "finishing it must not leave the claim held"

    assert (await read(client, repo))["items"] == [], "a done item is not work"
    history = await read(client, repo, include_done=True)
    # Filtered to this repo: a read for one repo also carries the fleet-wide
    # items, which is the point of fleet scope and would otherwise make this
    # assertion about every other test's leftovers.
    mine = [i for i in history["items"] if i["repo"] == repo]
    assert [i["state"] for i in mine] == ["done"]
    assert mine[0]["done_by"] == "laptop"


async def test_history_sorts_below_live_work(client):
    """A finished item keeps the rank it had, so a history read would otherwise
    lead with work from three weeks ago — and could push the live items past
    `limit` entirely."""
    repo = "acme/historyorder"
    first = await issue(client, repo, 90210)
    second = await issue(client, repo, 90211)
    await client.post("/plan/item/done", json={"item_id": first["item_id"]}, headers=LAPTOP)

    history = await read(client, repo, include_done=True)
    mine = [i["item_id"] for i in history["items"] if i["repo"] == repo]
    assert mine == [second["item_id"], first["item_id"]]


async def test_done_leaves_another_agents_claim_alone_and_says_so(client):
    """An item one agent holds might be finished by a third; taking somebody
    else's claim away is still not this endpoint's business."""
    repo = "acme/donenotmine"
    item = await issue(client, repo, 71)
    await take(client, item["item_id"], headers=SERVER, session="s-server")

    r = await client.post("/plan/item/done", json={"item_id": item["item_id"]},
                          headers=DESKTOP)
    assert r.status_code == 200, r.text
    # The claim itself, because a done item no longer renders one: "server was
    # still holding this when desktop recorded it finished" has to be readable
    # somewhere, and this is the only place left.
    assert r.json()["claim_left"]["holder"] == "server"
    assert r.json()["claim_left"]["session"] == "s-server"
    assert r.json()["state"] == "done"


async def test_a_done_item_frees_its_issue_for_a_new_item(client):
    """The uniqueness rule is about the plan holding one OPEN opinion per issue.
    Reopened work gets a fresh item; history keeps the old one."""
    repo = "acme/reopen"
    first = await issue(client, repo, 80)
    await client.post("/plan/item/done", json={"item_id": first["item_id"]}, headers=LAPTOP)

    second = await issue(client, repo, 80)
    assert second["item_id"] != first["item_id"]


async def test_a_claimed_item_cannot_be_claimed_after_it_is_done(client):
    repo = "acme/doneclaim"
    item = await issue(client, repo, 81)
    await client.post("/plan/item/done", json={"item_id": item["item_id"]}, headers=LAPTOP)
    r = await claim_item(client, item["item_id"], headers=DESKTOP)
    assert r.status_code == 409 and "done" in r.json()["detail"]["error"]


# --------------------------------------------------------- only a human orders

async def test_an_agent_may_not_reorder_the_plan(client):
    """Decision 1 of the issue, and the reason there is a `human` dependency at
    all: if any agent may reorder, the plan thrashes and stops being shared
    intent. The refusal says what to do instead."""
    repo = "acme/order"
    first = await issue(client, repo, 90)
    second = await issue(client, repo, 91)

    r = await client.post("/plan/reorder",
                          json={"repo": repo, "order": [second["item_id"], first["item_id"]]},
                          headers=LAPTOP)
    assert r.status_code == 403
    assert "human-only" in r.json()["detail"]

    ok = await client.post("/plan/reorder",
                           json={"repo": repo, "order": [second["item_id"], first["item_id"]]},
                           headers=HUMAN)
    assert ok.status_code == 200, ok.text
    assert (await read(client, repo))["next"]["item_id"] == second["item_id"]


async def test_a_reorder_never_loses_an_item_the_page_did_not_know_about(client):
    """A stale board page must not be able to drop an item added since it
    loaded — the omission is reported instead of assumed."""
    repo = "acme/partial"
    first = await issue(client, repo, 100)
    second = await issue(client, repo, 101)
    third = await issue(client, repo, 102)

    r = await client.post("/plan/reorder",
                          json={"repo": repo, "order": [third["item_id"], first["item_id"]]},
                          headers=HUMAN)
    assert r.status_code == 200, r.text
    assert r.json()["appended"] == [second["item_id"]]
    assert [i["item_id"] for i in (await read(client, repo))["items"]] == [
        third["item_id"], first["item_id"], second["item_id"]]


async def test_a_reorder_refuses_items_from_another_scope(client):
    """The write scope is exact where the read scope is generous: a rewrite that
    silently renumbered another repo's list would be the plan reordering itself
    behind your back."""
    mine = await issue(client, "acme/scopea", 110)
    theirs = await issue(client, "acme/scopeb", 111)

    r = await client.post("/plan/reorder",
                          json={"repo": "acme/scopea",
                                "order": [theirs["item_id"], mine["item_id"]]},
                          headers=HUMAN)
    assert r.status_code == 422
    assert r.json()["detail"]["items"] == [theirs["item_id"]]


async def test_only_a_human_may_drop_or_retitle_an_item(client):
    """Deciding something should not be done is the same class of decision as
    deciding what comes first."""
    repo = "acme/drop"
    item = await issue(client, repo, 120)

    r = await client.post("/plan/item/update",
                          json={"item_id": item["item_id"], "state": "dropped"},
                          headers=LAPTOP)
    assert r.status_code == 403

    ok = await client.post("/plan/item/update",
                           json={"item_id": item["item_id"], "state": "dropped",
                                 "note": "superseded by #121"},
                           headers=HUMAN)
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "dropped" and ok.json()["edited_by"] == "rich"
    assert (await read(client, repo))["items"] == []


async def test_an_agent_cannot_finish_what_a_human_dropped(client):
    """Otherwise the human-only rule is one call away from being routed around:
    a drop says this should not happen, and `done` would say it did."""
    repo = "acme/dropdone"
    item = await issue(client, repo, 131)
    await client.post("/plan/item/update",
                      json={"item_id": item["item_id"], "state": "dropped"}, headers=HUMAN)

    r = await client.post("/plan/item/done", json={"item_id": item["item_id"]},
                          headers=LAPTOP)
    assert r.status_code == 409 and "dropped" in r.json()["detail"]["error"]


async def test_a_dropped_item_can_be_put_back(client):
    repo = "acme/undrop"
    item = await issue(client, repo, 130)
    await client.post("/plan/item/update",
                      json={"item_id": item["item_id"], "state": "dropped"}, headers=HUMAN)
    r = await client.post("/plan/item/update",
                          json={"item_id": item["item_id"], "state": "open"}, headers=HUMAN)
    assert r.status_code == 200 and r.json()["state"] == "open"
    assert (await read(client, repo))["next"]["item_id"] == item["item_id"]


# --------------------------------------------------------------- fleet + stale

async def test_a_fleet_item_shows_in_every_repos_plan(client):
    """The plan spans repos, as does the fleet: "rebuild home-manager on every
    box" belongs to no repo, and an agent that never sees it is exactly the
    agent fleet scope is for."""
    fleet = await add(client, None, "rebuild home-manager everywhere")
    repo_item = await issue(client, "acme/fleetread", 140)

    ids = [i["item_id"] for i in (await read(client, "acme/fleetread"))["items"]]
    assert fleet["item_id"] in ids and repo_item["item_id"] in ids
    assert fleet["repo"] is None
    # Finished for the same reason the other two fleet tests finish theirs: a
    # fleet item is in EVERY repo's read, so an open one left here is a row in
    # every later test's scope. It survived only because nothing ran after it.
    await client.post("/plan/item/done", json={"item_id": fleet["item_id"]}, headers=LAPTOP)


def test_staleness_is_reported_rather_than_left_to_the_reader():
    """A plan nobody updates is worse than none, because it is believed. Pure
    view logic, so the boundary is asserted without waiting a fortnight."""
    now = datetime.now(UTC)
    fresh = PlanItem(id=uuid.uuid4(), title="fresh", state="open", rank=1,
                     depends_on=[], added_by="laptop", created_at=now, updated_at=now)
    old = PlanItem(id=uuid.uuid4(), title="old", state="open", rank=2, depends_on=[],
                   added_by="laptop", created_at=now,
                   updated_at=now - timedelta(days=STALE_DAYS, seconds=1))
    done = PlanItem(id=uuid.uuid4(), title="done", state="done", rank=3, depends_on=[],
                    added_by="laptop", created_at=now,
                    updated_at=now - timedelta(days=365))

    assert _item_view(fresh, None, [], now)["stale"] is False
    assert _item_view(old, None, [], now)["stale"] is True
    assert _item_view(old, None, [], now)["idle_days"] >= STALE_DAYS
    # A finished item is not stale, it is history — flagging it would make the
    # count meaningless the week after any release.
    assert _item_view(done, None, [], now)["stale"] is False


# ------------------------------------------------- the human/agent boundary

async def test_a_remote_user_header_alone_is_not_a_person(client):
    """The boundary the whole human-only split rests on, enforced rather than
    documented. Every agent on a box holds the same machine token, so if the one
    thing dividing "an agent" from "a person" is a header any caller can set,
    the split is one extra header wide — and the deployment note that says the
    edge strips it lives in a config file this repo does not ship."""
    repo = "acme/spoof"
    first = await issue(client, repo, 500)
    second = await issue(client, repo, 501)
    order = {"repo": repo, "order": [second["item_id"], first["item_id"]]}

    forged = await client.post("/plan/reorder", json=order, headers=SPOOFED)
    assert forged.status_code == 403
    assert "not asserted by the edge" in forged.json()["detail"]

    wrong = await client.post("/plan/reorder", json=order,
                              headers={**SPOOFED, "X-Edge-Auth": "not-the-secret"})
    assert wrong.status_code == 403, "a near-miss secret is a miss"

    ok = await client.post("/plan/reorder", json=order, headers=HUMAN)
    assert ok.status_code == 200, ok.text


async def test_an_agent_token_plus_a_forged_edge_header_is_still_an_agent(client):
    """The traffic shape that makes this reachable: a bypass rule that skips
    forward-auth for bearer-authenticated API paths is normal, and it is exactly
    what agents send."""
    repo = "acme/spoofbearer"
    item = await issue(client, repo, 502)
    r = await client.post("/plan/item/update",
                          json={"item_id": item["item_id"], "state": "dropped"},
                          headers={**LAPTOP, **SPOOFED})
    assert r.status_code == 403
    assert (await read(client, repo))["items"][0]["state"] == "open"


async def test_the_dev_browser_bypass_reads_but_does_not_decide(client, monkeypatch):
    """`BROWSER_DEV_USER` is `reader`'s bypass, and reading is not deciding. It
    used to grant the human-only writes to any unauthenticated caller — on a
    reachable instance, to every agent on the box — because the same flag was
    doing two different jobs."""
    repo = "acme/devuser"
    item = await issue(client, repo, 503)
    monkeypatch.setattr(settings, "browser_dev_user", "devuser")

    assert (await client.get("/plan", params={"repo": repo})).status_code == 200
    r = await client.post("/plan/item/update",
                          json={"item_id": item["item_id"], "state": "dropped"})
    assert r.status_code == 401, "a read bypass is not an authorisation"

    # ...and the local board that wants the buttons says so, deliberately.
    monkeypatch.setattr(settings, "browser_dev_human", True)
    ok = await client.post("/plan/item/update",
                           json={"item_id": item["item_id"], "state": "dropped"})
    assert ok.status_code == 200 and ok.json()["edited_by"] == "devuser"


# --------------------------------------- `next` is about the plan, not the page

async def test_the_limit_pages_the_list_and_never_the_answer(client):
    """`limit` truncates the page; it used to truncate the question. With the
    first `limit` items claimed or blocked, the endpoint answered "nothing is
    free" while free work sat one rank below the cut — the single failure the
    whole feature exists to prevent."""
    repo = "acme/limitnext"
    held = await issue(client, repo, 600)
    blocked = await issue(client, repo, 601, depends_on=["#600"])
    free = await issue(client, repo, 602)
    await take(client, held["item_id"], session="s-1")

    plan = await read(client, repo, limit=1)
    assert [i["item_id"] for i in plan["items"]] == [held["item_id"]]
    assert plan["truncated"] is True
    assert plan["next"]["item_id"] == free["item_id"], "the answer is not paged"
    # ...and the counts are the plan's, not the page's: the board header renders
    # them verbatim, so under-reporting them is a wrong number a human reads.
    assert plan["counts"]["open"] >= 3
    assert plan["counts"]["claimed"] >= 1 and plan["counts"]["blocked"] >= 1
    assert blocked["item_id"] not in [i["item_id"] for i in plan["items"]]


async def test_history_counts_are_the_scopes_and_not_the_pages(client):
    repo = "acme/counthistory"
    done_one = await issue(client, repo, 610)
    await issue(client, repo, 611)
    await client.post("/plan/item/done", json={"item_id": done_one["item_id"]},
                      headers=LAPTOP)

    plan = await read(client, repo, include_done=True, limit=1)
    assert len(plan["items"]) == 1 and plan["truncated"] is True
    assert plan["counts"]["done"] >= 1 and plan["counts"]["open"] >= 1


async def test_a_repos_own_list_comes_before_the_fleets(client):
    """Ranks are allocated per scope, so merging two independent 1..n sequences
    by rank alone interleaved orders nobody had ever compared. The rule is
    stated instead: your repo first, and the fleet list is what you pick up when
    your own has nothing free."""
    repo = "acme/bands"
    fleet = await add(client, None, "rebuild everything everywhere")
    mine = await issue(client, repo, 620)

    plan = await read(client, repo)
    ids = [i["item_id"] for i in plan["items"]]
    assert ids.index(mine["item_id"]) < ids.index(fleet["item_id"])
    assert plan["next"]["item_id"] == mine["item_id"]

    # ...and it falls through into the fleet band rather than starving it.
    await take(client, mine["item_id"], session="s-1")
    assert (await read(client, repo))["next"]["item_id"] == fleet["item_id"]
    await client.post("/plan/item/done", json={"item_id": fleet["item_id"]}, headers=LAPTOP)


# ------------------------------------------- one worker is not one machine

async def test_the_fleet_list_can_be_read_by_itself(client):
    """The board page's fleet view is a read, not a filter. Narrowing a wider
    read in the browser made the header describe one set and the list another —
    and, with the fleet band sorting last, `limit` could cut off the very rows
    the view exists to show."""
    repo = "acme/exactread"
    mine = await issue(client, repo, 625)
    fleet = await add(client, None, "fleet-only read")

    only_fleet = await read(client, exact="true")
    ids = [i["item_id"] for i in only_fleet["items"]]
    assert fleet["item_id"] in ids and mine["item_id"] not in ids
    assert only_fleet["counts"]["open"] == len(only_fleet["items"])
    assert only_fleet["next"]["item_id"] in ids

    just_mine = await read(client, repo, exact="true")
    assert [i["item_id"] for i in just_mine["items"]] == [mine["item_id"]]
    await client.post("/plan/item/done", json={"item_id": fleet["item_id"]}, headers=LAPTOP)


async def test_two_agents_on_one_machine_cannot_both_hold_an_item(client):
    """The failure this feature exists to prevent, moved indoors. A claim
    belongs to the box for a land — an agent that restarts must reclaim its
    own — but a machine runs several agents at once and they all authenticate
    as that one token, so the box rule told the second one `renewed: true`."""
    repo = "acme/samebox"
    item = await issue(client, repo, 630)
    await take(client, item["item_id"], session="s-first", note="on it")

    contended = await claim_item(client, item["item_id"], session="s-second")
    assert contended.status_code == 409, "a second session is a second worker"
    assert contended.json()["detail"]["session"] == "s-first"

    mine_again = await take(client, item["item_id"], session="s-first")
    assert mine_again["renewed"] is True, "my own session still renews"


async def test_a_co_tenant_cannot_release_or_finish_your_item(client):
    repo = "acme/sameboxrelease"
    item = await issue(client, repo, 631)
    await take(client, item["item_id"], session="s-mine")

    r = await client.post("/plan/item/release",
                          json={"item_id": item["item_id"], "session": "s-theirs"},
                          headers=LAPTOP)
    assert r.status_code == 403 and r.json()["detail"]["session"] == "s-mine"

    done = await client.post("/plan/item/done",
                             json={"item_id": item["item_id"], "session": "s-theirs"},
                             headers=LAPTOP)
    assert done.status_code == 200, "recording that the issue closed is anyone's"
    assert done.json()["claim_left"]["session"] == "s-mine", "their claim is theirs"


async def test_a_claim_that_named_no_session_still_belongs_to_the_box(client):
    """Nothing finer was recorded, so the machine is all there is to compare —
    refusing outright would strand every claim taken by a caller that sent none.
    """
    repo = "acme/nosession"
    item = await issue(client, repo, 632)
    await take(client, item["item_id"])
    r = await client.post("/plan/item/release",
                          json={"item_id": item["item_id"], "session": "s-later"},
                          headers=LAPTOP)
    assert r.status_code == 200 and r.json()["released"] is True


# ----------------------------------------- terminal states and their claims

async def test_dropping_an_item_frees_whoever_was_holding_it(client):
    """A human deciding this should not happen has to reach the agent doing it:
    the item vanishes from every read, so a claim left live on its key is one
    nobody can see, blocking the issue's next item until the TTL runs out."""
    repo = "acme/dropclaim"
    item = await issue(client, repo, 640)
    await take(client, item["item_id"], headers=SERVER, session="s-server")

    r = await client.post("/plan/item/update",
                          json={"item_id": item["item_id"], "state": "dropped"},
                          headers=HUMAN)
    assert r.status_code == 200, r.text
    async with async_session() as s:
        live = await s.scalar(select(ResourceLease).where(
            ResourceLease.kind == CLAIM_KIND, ResourceLease.key == f"{repo}#640",
            ResourceLease.released_at.is_(None)))
    assert live is None, "a cancelled item must not stay held"


async def test_a_done_item_cannot_be_dropped_out_of_its_own_history(client):
    """Dropping clears done_at and done_by, so the drop control on a history row
    was one click from erasing the record that the issue ever closed."""
    repo = "acme/donedrop"
    item = await issue(client, repo, 641)
    await client.post("/plan/item/done", json={"item_id": item["item_id"]}, headers=LAPTOP)

    r = await client.post("/plan/item/update",
                          json={"item_id": item["item_id"], "state": "dropped"},
                          headers=HUMAN)
    assert r.status_code == 409 and "record" in r.json()["detail"]["error"]
    history = await read(client, repo, include_done=True)
    mine = next(i for i in history["items"] if i["item_id"] == item["item_id"])
    assert mine["state"] == "done" and mine["done_by"] == "laptop"


async def test_a_live_claim_is_never_shown_on_finished_history(client):
    """Claims are keyed by repo#issue, so a re-added item shares its key with the
    one it replaced — and the history read showed the new item's live claim
    sitting on the old done row, which reads as work being done twice."""
    repo = "acme/refreuse"
    first = await issue(client, repo, 650)
    await client.post("/plan/item/done", json={"item_id": first["item_id"]}, headers=LAPTOP)
    second = await issue(client, repo, 650)
    await take(client, second["item_id"], session="s-again")

    history = await read(client, repo, include_done=True)
    by_id = {i["item_id"]: i for i in history["items"]}
    assert by_id[second["item_id"]]["claim"]["session"] == "s-again"
    assert by_id[first["item_id"]]["claim"] is None


async def test_an_item_finished_mid_claim_does_not_stay_claimed(client):
    """The state check and the claim are two statements and nothing can lock
    across them — `acquire` commits, which is where its atomicity comes from —
    so the check is made again afterwards and a claim that lost is handed back.
    """
    repo = "acme/racedone"
    item = await issue(client, repo, 651)
    claiming = asyncio.create_task(
        claim_item(client, item["item_id"], session="s-race"))
    finishing = asyncio.create_task(
        client.post("/plan/item/done", json={"item_id": item["item_id"]}, headers=DESKTOP))
    claimed, _ = await asyncio.gather(claiming, finishing)

    if claimed.status_code == 200:
        return  # it won the race outright; nothing to leave behind
    assert claimed.status_code == 409
    async with async_session() as s:
        live = await s.scalar(select(ResourceLease).where(
            ResourceLease.kind == CLAIM_KIND, ResourceLease.key == f"{repo}#651",
            ResourceLease.released_at.is_(None)))
    assert live is None, "a claim on a finished item is a claim nobody can act on"


async def test_releasing_nothing_does_not_reset_the_staleness_clock(client):
    """`updated_at` is the only input to `stale`, so a poller calling release on
    an item it never held could keep an abandoned one looking fresh forever —
    hiding precisely the item the flag exists to surface."""
    repo = "acme/staleclock"
    item = await issue(client, repo, 660)
    before = (await read(client, repo))["items"][0]["updated"]

    r = await client.post("/plan/item/release",
                          json={"item_id": item["item_id"]}, headers=DESKTOP)
    assert r.status_code == 200 and r.json()["released"] is False
    assert (await read(client, repo))["items"][0]["updated"] == before


async def test_a_forced_claim_says_so_in_the_record(client):
    """"The refusal is advice, but it has to be said out loud" — and it was said
    to nobody: a forced claim and an ordinary one were indistinguishable an hour
    later."""
    repo = "acme/forcednote"
    await issue(client, repo, 670)
    waiter = await issue(client, repo, 671, depends_on=["#670"])
    forced = await take(client, waiter["item_id"], force=True, note="doing it anyway",
                        session="s-force")
    assert forced["claim"]["note"].startswith("[forced past #670]")
    assert "doing it anyway" in forced["claim"]["note"]
    assert forced["forced"] is True


async def test_the_completion_note_is_added_to_the_human_note_not_over_it(client):
    """`note` is why the item sits where it sits — human-only to edit for that
    very reason — and a completing agent's receipt used to replace it."""
    repo = "acme/notekeep"
    item = await issue(client, repo, 680, note="before #53: its schema is what #53 queries")
    r = await client.post("/plan/item/done",
                          json={"item_id": item["item_id"], "note": "landed in PR #143"},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["note"].startswith("before #53")
    assert "landed in PR #143" in r.json()["note"]


# --------------------------------------------- order is a total order, still

async def test_two_reorders_of_one_scope_cannot_interleave(client):
    """Read-rewrite-commit with nothing between them: two board tabs both read
    the same list and both wrote, so the ranks could end up neither order."""
    repo = "acme/reorderrace"
    a = await issue(client, repo, 700)
    b = await issue(client, repo, 701)
    c = await issue(client, repo, 702)
    forward = [a["item_id"], b["item_id"], c["item_id"]]
    backward = list(reversed(forward))

    both = await asyncio.gather(
        client.post("/plan/reorder", json={"repo": repo, "order": forward}, headers=HUMAN),
        client.post("/plan/reorder", json={"repo": repo, "order": backward}, headers=HUMAN),
    )
    assert [r.status_code for r in both] == [200, 200], [r.text for r in both]
    landed = [i["item_id"] for i in (await read(client, repo))["items"]]
    assert landed in (forward, backward), "one order or the other, never a blend"


async def test_two_adds_in_one_scope_do_not_land_on_one_rank(client):
    """`_next_rank` is a read-then-insert and there is no unique index on
    (repo, rank) to notice the collision — two items at one position, ordered
    thereafter by whichever was created first."""
    repo = "acme/addrace"
    both = await asyncio.gather(
        add(client, repo, "first"), add(client, repo, "second", headers=DESKTOP))
    ranks = sorted(i["rank"] for i in both)
    assert ranks == [1, 2], f"one rank each, got {ranks}"


async def test_a_reorder_will_not_renumber_history(client):
    """Dropped rows used to be re-ranked by every reorder and named in
    `appended` while being absent from the `items` the same response returned."""
    repo = "acme/reorderdropped"
    live = await issue(client, repo, 710)
    dropped = await issue(client, repo, 711)
    await client.post("/plan/item/update",
                      json={"item_id": dropped["item_id"], "state": "dropped"},
                      headers=HUMAN)

    r = await client.post("/plan/reorder",
                          json={"repo": repo, "order": [dropped["item_id"]]},
                          headers=HUMAN)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["items"] == [dropped["item_id"]]

    ok = await client.post("/plan/reorder",
                           json={"repo": repo, "order": [live["item_id"]]}, headers=HUMAN)
    assert ok.status_code == 200 and ok.json()["appended"] == []
    assert [i["item_id"] for i in ok.json()["items"]] == [live["item_id"]]


# ------------------------------------- placing is not reordering (#183)


async def test_an_agent_may_say_where_a_new_item_enters(client):
    """The premise the endpoint was documented on, made true. "Adding is not
    reordering" was right about adding and false about what the code did: there
    was no way to add without also deciding where it went, and "last" was
    hard-coded — an ordering judgement asserted on the caller's behalf."""
    repo = "acme/place"
    first = await issue(client, repo, 800)
    second = await issue(client, repo, 801)
    urgent = await add(client, repo, "told this is near-top", before=second["item_id"])

    assert urgent["rank"] == 2 and urgent["rank_source"] == "placed"
    assert [i["ref"]["value"] if i["ref"] else i["title"]
            for i in (await read(client, repo))["items"]] == ["800", "told this is near-top", "801"]
    assert first["item_id"] and (await read(client, repo))["items"][0]["rank"] == 1


async def test_placing_changes_the_relative_order_of_nothing_already_there(client):
    """The whole reason this is agent-permitted. Reordering is contested because
    two agents can overwrite each other's decision; a placement overwrites none,
    because every existing pair keeps the relationship it had."""
    repo = "acme/placepairs"
    before_ids = [(await issue(client, repo, n))["item_id"] for n in (810, 811, 812)]
    await add(client, repo, "wedged in", after="#810")

    after = [i["item_id"] for i in (await read(client, repo))["items"]]
    assert [i for i in after if i in before_ids] == before_ids, \
        "an existing pair changed order — that is a reorder, and agents may not"


async def test_a_position_may_name_the_issue_rather_than_the_item(client):
    """An agent transcribing a spoken priority has an issue number, not a uuid —
    which is why `depends_on` takes both spellings too."""
    repo = "acme/placeref"
    anchor = await issue(client, repo, 820)
    await issue(client, repo, 821)
    placed = await add(client, repo, "beneath 820", after="#820")

    assert placed["rank"] == anchor["rank"] + 1
    assert [i["title"] for i in (await read(client, repo, exact="true"))["items"]] == [
        "#820", "beneath 820", "#821"]


async def test_a_position_is_one_neighbour_and_not_two(client):
    repo = "acme/placeboth"
    a = await issue(client, repo, 830)
    b = await issue(client, repo, 831)
    r = await client.post("/plan/item",
                          json={"repo": repo, "title": "confused",
                                "after": a["item_id"], "before": b["item_id"]},
                          headers=LAPTOP)
    assert r.status_code == 422
    assert "not both" in r.json()["detail"]["error"]


async def test_an_empty_position_is_no_position_rather_than_two(client):
    """`after=""` names nothing, so a request carrying it alongside a real
    `before` names one position — refusing it as "two" would refuse a request
    that is not ambiguous."""
    repo = "acme/placeblank"
    anchor = await issue(client, repo, 835)
    r = await client.post("/plan/item",
                          json={"repo": repo, "title": "above it",
                                "after": "", "before": anchor["item_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["rank"] == 1 and r.json()["rank_source"] == "placed"


async def test_a_position_in_another_scope_is_refused(client):
    """Ranks are allocated per scope: a repo's list is 1..n and the fleet's is
    its own 1..n, so "after the fleet item ranked 3" names a position in a
    sequence this item is not in."""
    fleet = await add(client, None, "fleet-wide thing")
    r = await client.post("/plan/item",
                          json={"repo": "acme/placescope", "title": "mine",
                                "after": fleet["item_id"]},
                          headers=LAPTOP)
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "after"
    assert "fleet" in r.json()["detail"]["error"]
    # And the other way round, because the widened READ is what makes this
    # tempting: a repo read carries the fleet band along, so a fleet item can see
    # a repo item in the list it was handed and still not share its sequence.
    mine = await issue(client, "acme/placescope", 833)
    back = await client.post("/plan/item",
                             json={"title": "fleet, placed at a repo item",
                                   "before": mine["item_id"]},
                             headers=LAPTOP)
    assert back.status_code == 422 and back.json()["detail"]["field"] == "before"
    # Finished on the way out, as the fleet tests above do: an open fleet item is
    # a row in EVERY later test's scope.
    await client.post("/plan/item/done", json={"item_id": fleet["item_id"]},
                      headers=LAPTOP)


async def test_a_position_beside_finished_work_is_refused(client):
    """Only open items carry an order — the same rule a reorder enforces, said
    the same way, because a done row is a record and not a place."""
    repo = "acme/placedone"
    done = await issue(client, repo, 840)
    await client.post("/plan/item/done", json={"item_id": done["item_id"]}, headers=LAPTOP)

    r = await client.post("/plan/item",
                          json={"repo": repo, "title": "next to history",
                                "after": done["item_id"]},
                          headers=LAPTOP)
    assert r.status_code == 422
    assert "done" in r.json()["detail"]["error"]


async def test_a_position_beside_nothing_is_refused_by_either_spelling(client):
    repo = "acme/placemissing"
    await issue(client, repo, 845)
    by_id = await client.post("/plan/item",
                              json={"repo": repo, "title": "x",
                                    "after": str(uuid.uuid4())}, headers=LAPTOP)
    assert by_id.status_code == 422 and "no such plan item" in by_id.json()["detail"]["error"]

    by_ref = await client.post("/plan/item",
                               json={"repo": repo, "title": "x", "before": "#999"},
                               headers=LAPTOP)
    assert by_ref.status_code == 422
    assert "references that issue" in by_ref.json()["detail"]["error"]


async def test_placing_does_not_renumber_history(client):
    """A finished item keeps the rank it had, exactly as a reorder leaves it
    alone: shifting it would rewrite the completion record of work that is over
    every time somebody placed an item above it."""
    repo = "acme/placehistory"
    done = await issue(client, repo, 850)
    live = await issue(client, repo, 851)
    await client.post("/plan/item/done", json={"item_id": done["item_id"]}, headers=LAPTOP)
    was = next(i for i in (await read(client, repo, include_done=True))["items"]
               if i["item_id"] == done["item_id"])["rank"]

    await add(client, repo, "above the live one", before=live["item_id"])
    still = next(i for i in (await read(client, repo, include_done=True))["items"]
                 if i["item_id"] == done["item_id"])["rank"]
    assert still == was


async def test_placing_does_not_reset_the_staleness_clock_of_what_it_moves(client):
    """`updated_at` is "has anybody paid this item attention", and being
    renumbered is not attention. One placement must not make a fortnight-old
    plan read as fresh — that is precisely the plan that is believed and wrong."""
    repo = "acme/placestale"
    old = await issue(client, repo, 860)
    before = (await read(client, repo))["items"][0]["updated"]

    await add(client, repo, "in front of it", before=old["item_id"])
    moved = next(i for i in (await read(client, repo))["items"]
                 if i["item_id"] == old["item_id"])
    assert moved["rank"] == 2 and moved["updated"] == before


async def test_two_placements_in_one_scope_do_not_land_on_one_rank(client):
    """`_place_rank` reads a rank and shifts from it — the same read-then-write
    `_next_rank` is, and the same lost update if two of them interleave."""
    repo = "acme/placerace"
    anchor = await issue(client, repo, 870)
    both = await asyncio.gather(
        add(client, repo, "one", before=anchor["item_id"]),
        add(client, repo, "two", before=anchor["item_id"], headers=DESKTOP))
    assert [r["rank_source"] for r in both] == ["placed", "placed"]
    ranks = sorted(i["rank"] for i in (await read(client, repo, exact="true"))["items"])
    assert ranks == [1, 2, 3], f"one rank each, got {ranks}"


async def test_a_placement_records_whose_priority_it_transcribes(client):
    """The field the workaround had to invent. Told mid-seed that #85 was
    near-top, the agent wrote "TOP PRIORITY — Rich, 23:00" into `phase` and
    "RANK IS WRONG AND A HUMAN MUST FIX IT" into `note`, because those were the
    only writable strings."""
    repo = "acme/placedfor"
    anchor = await issue(client, repo, 880)
    placed = await add(client, repo, "the appetite gate", before=anchor["item_id"],
                       placed_for="Rich, 2026-08-17 23:00")
    assert placed["placed_for"] == "Rich, 2026-08-17 23:00"
    assert placed["rank_source"] == "placed"


async def test_provenance_without_a_position_is_refused(client):
    """On its own it would be one more free-text priority channel, which is the
    workaround rather than the fix."""
    r = await client.post("/plan/item",
                          json={"repo": "acme/placedfor2", "title": "urgent, honest",
                                "placed_for": "Rich, 23:00"},
                          headers=LAPTOP)
    assert r.status_code == 422
    assert "needs `after` or `before`" in r.json()["detail"]["error"]


async def test_an_appended_item_still_appends_and_says_nobody_chose_it(client):
    """Nothing that worked before changes — a position is optional, and its
    absence is now recorded rather than silently meaning "least important"."""
    repo = "acme/placedefault"
    await issue(client, repo, 890)
    plain = await add(client, repo, "no position given")
    assert plain["rank"] == 2 and plain["rank_source"] == "appended"
    assert plain["placed_for"] is None


# ------------------------------- `next` says how good an answer it is (#183)


async def test_next_admits_when_the_order_is_one_nobody_chose(client):
    """The sharpest complaint in the issue: `plan_read` returned `next` = rank 1
    confidently while the human's stated top priority sat at rank 20 under a note
    shouting that the rank was a lie. Every signal was in free text."""
    repo = "acme/untrusted"
    await issue(client, repo, 900)
    await issue(client, repo, 901)

    plan = await read(client, repo, exact="true")
    assert plan["order_trust"]["trusted"] is False
    assert plan["order_trust"]["unchosen"] == 2
    assert plan["order_trust"]["first_unchosen"]["rank"] == 1
    assert plan["order_trust"]["first_unchosen"]["repo"] == repo
    assert plan["order_trust"]["by_source"] == {"appended": 2}
    assert "nobody chose" in plan["next"]["caveat"]


async def test_a_human_ordering_the_list_is_what_makes_next_confident(client):
    """`trusted` is not decoration: it flips when somebody actually decides, and
    the caveat goes with it."""
    repo = "acme/trusted"
    first = await issue(client, repo, 910)
    second = await issue(client, repo, 911)
    r = await client.post("/plan/reorder",
                          json={"repo": repo, "order": [second["item_id"], first["item_id"]]},
                          headers=HUMAN)
    assert r.status_code == 200, r.text

    plan = await read(client, repo, exact="true")
    assert plan["order_trust"] == {"trusted": True, "by_source": {"ordered": 2},
                                "unchosen": 0, "first_unchosen": None, "hint": None}
    assert plan["next"]["caveat"] is None
    assert plan["next"]["rank_source"] == "ordered"


async def test_a_reorder_claims_no_decision_about_an_item_the_page_never_saw(client):
    """A stale page carries an unseen item along rather than losing it — and
    carrying it is not deciding where it goes. Marking it `ordered` would make
    the plan claim a human chose a position they were never shown."""
    repo = "acme/orderedrest"
    first = await issue(client, repo, 920)
    second = await issue(client, repo, 921)
    unseen = await issue(client, repo, 922)

    r = await client.post("/plan/reorder",
                          json={"repo": repo, "order": [second["item_id"], first["item_id"]]},
                          headers=HUMAN)
    assert r.json()["appended"] == [unseen["item_id"]]
    plan = await read(client, repo, exact="true")
    assert plan["order_trust"]["by_source"] == {"ordered": 2, "appended": 1}
    assert plan["order_trust"]["trusted"] is False
    assert plan["order_trust"]["first_unchosen"]["rank"] == 3
    assert plan["next"]["caveat"] is not None
    assert " this one among them," not in plan["next"]["caveat"], \
        "next itself was ordered by a human — the caveat is about the rest"


async def test_a_placed_item_is_a_chosen_position(client):
    """Placement is what an agent can do about an untrustworthy order without
    reordering anything: a plan every item of which was placed is trusted."""
    repo = "acme/placedtrust"
    anchor = await issue(client, repo, 930)
    await client.post("/plan/reorder", json={"repo": repo, "order": [anchor["item_id"]]},
                      headers=HUMAN)
    await add(client, repo, "placed above it", before=anchor["item_id"])

    plan = await read(client, repo, exact="true")
    assert plan["order_trust"]["by_source"] == {"placed": 1, "ordered": 1}
    assert plan["order_trust"]["trusted"] is True and plan["next"]["caveat"] is None


async def test_the_caveat_names_the_answers_own_position_when_that_is_the_problem(client):
    """"Read the notes" is different advice depending on whether the item you
    are being handed is itself sitting where nobody put it."""
    repo = "acme/caveatself"
    await issue(client, repo, 940)
    plan = await read(client, repo, exact="true")
    assert " this one among them," in plan["next"]["caveat"]


async def test_an_empty_scope_is_trusted_rather_than_suspicious(client):
    """Nothing unchosen is nothing unchosen. A flag that fires on an empty plan
    is one every reader learns to ignore."""
    plan = await read(client, "acme/emptyorder", exact="true")
    assert plan["order_trust"]["trusted"] is True and plan["next"] is None
    assert plan["order_trust"]["by_source"] == {}


# --------------------------------------------------- spelling and edge cases

async def test_one_issue_is_one_item_however_the_repo_is_spelled(client):
    """GitHub repository names are case-insensitive, so `Acme/Repo#60` and
    `acme/repo#60` are one issue everywhere except a table that never
    lower-cased them — where they were two open items with two claim keys."""
    first = await issue(client, "Acme/CaseRepo", 720)
    assert first["repo"] == "acme/caserepo"
    dup = await client.post("/plan/item", json={
        "repo": "acme/caserepo", "title": "same issue, other spelling",
        "ref_kind": "issue", "ref_value": "720"}, headers=DESKTOP)
    assert dup.status_code == 409
    assert (await read(client, "ACME/CaseRepo"))["items"][0]["item_id"] == first["item_id"]


async def test_a_title_of_spaces_is_not_a_title(client):
    r = await client.post("/plan/item", json={"repo": "acme/blank", "title": "   "},
                          headers=LAPTOP)
    assert r.status_code == 422


async def test_a_dependency_on_a_dropped_item_is_refused_by_either_spelling(client):
    """`#20` was refused and the same item's uuid was accepted, storing an edge
    that could never block and never show — a 200 for nothing."""
    repo = "acme/deadedge"
    blocker = await issue(client, repo, 730)
    waiter = await issue(client, repo, 731)
    await client.post("/plan/item/update",
                      json={"item_id": blocker["item_id"], "state": "dropped"},
                      headers=HUMAN)

    for token in ("#730", blocker["item_id"]):
        r = await client.post("/plan/item/depends",
                              json={"item_id": waiter["item_id"], "depends_on": [token]},
                              headers=LAPTOP)
        assert r.status_code == 422, f"{token}: {r.text}"
        assert r.json()["detail"]["error"], "every refusal has the same shape"


async def test_history_records_no_new_dependencies(client):
    repo = "acme/depdone"
    blocker = await issue(client, repo, 740)
    finished = await issue(client, repo, 741)
    await client.post("/plan/item/done", json={"item_id": finished["item_id"]},
                      headers=LAPTOP)
    r = await client.post("/plan/item/depends",
                          json={"item_id": finished["item_id"],
                                "depends_on": [blocker["item_id"]]}, headers=LAPTOP)
    assert r.status_code == 409


# ------------------------------------------------------------- the primitive

async def test_acquire_refuses_a_session_with_work_in_flight():
    """It commits — that is where the atomicity comes from — so handing it a
    half-finished unit of work committed the half. Both callers happen to be
    clean today; the function was extracted precisely so more will exist."""
    async with async_session() as s:
        s.add(PlanItem(title="not yet saved", rank=1, depends_on=[], added_by="laptop"))
        with pytest.raises(RuntimeError, match="commits"):
            await acquire(s, ClaimRequest(kind="work", key="acme/x#1", holder="laptop"))
        await s.rollback()


async def test_acquire_canonicalises_inside_the_primitive_not_at_the_endpoint():
    """The successor to the reserved-kind guard, and the same lesson (#172).

    That guard sat in front of ONE caller of the primitive rather than inside it,
    so the next caller could write the row it was meant to prevent. Key
    derivation had exactly that shape available to it — canonicalise in the
    endpoint and let the plan router compose its own — so it happens in
    `ClaimRequest` instead, where no caller of `acquire` can be the one that
    forgot."""
    async with async_session() as s:
        claim, _ = await acquire(s, ClaimRequest(
            kind="issue", key="Acme/Canon#77", holder="laptop", sess="s-canon"))
    assert (claim.kind, claim.key) == (CLAIM_KIND, "acme/canon#77")


# ------------------------------------------------------------ the browser page

async def test_the_plan_page_is_served_to_a_reader(client):
    r = await client.get("/plan/view", headers=LAPTOP)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "plan" in r.text
    assert (await client.get("/plan/view")).status_code == 401


# ---------------------------------------------------------- the MCP surface

def _mcp_client(recorder, **over):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))
    from mcp_server.client import QuarterbackClient

    def handle(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"ok": True})

    return QuarterbackClient("http://board", "tok",
                              transport=httpx.MockTransport(handle), **over)


def test_the_mcp_client_stamps_the_session_on_every_plan_verb():
    """A claim whose holder cannot be reached is half a claim — and with the
    plan's claims owned by the session rather than the box, an unstamped one is
    also a claim its own agent cannot release."""
    seen: list[httpx.Request] = []
    client = _mcp_client(seen, session="s-42")
    client.plan_item("claim", {"item_id": "abc"})
    client.plan_item("release", {"item_id": "abc", "session": "s-mine"})

    assert seen[0].url.path == "/plan/item/claim"
    assert b'"session":"s-42"' in seen[0].content.replace(b" ", b"")
    assert b'"session":"s-mine"' in seen[1].content.replace(b" ", b"")


def test_the_mcp_client_sends_only_the_plan_filters_it_was_given():
    seen: list[httpx.Request] = []
    client = _mcp_client(seen)
    client.plan({"repo": "acme/x", "plan": None, "include_done": False, "limit": None})
    assert seen[0].url.params.get("repo") == "acme/x"
    assert "plan" not in seen[0].url.params and "limit" not in seen[0].url.params

    client.plan_add({"title": "t", "repo": None})
    assert seen[1].url.path == "/plan/item" and seen[1].method == "POST"


def test_every_plan_verb_the_mcp_tools_use_is_a_route_the_board_serves():
    """The MCP tools live in a package that does not ship with the server, so
    nothing but a test connects the two: a renamed endpoint would be discovered
    by an agent, at the moment it needed the plan."""
    from app.main import app as board

    paths = set(board.openapi()["paths"])
    for verb in ("claim", "release", "done", "depends"):
        assert f"/plan/item/{verb}" in paths
    assert {"/plan", "/plan/item", "/plan/reorder", "/plan/view"} <= paths


def test_the_mcp_plan_tools_teach_placing_and_the_caveat():
    """Agents learn this API from the tool docstring and nowhere else, and
    `plan_add`'s said "Adding is not reordering, so you may" — the premise #183
    is about, stated by the one surface every agent reads. A position an agent
    cannot pass is a position agents will keep faking in free text."""
    source = (Path(__file__).resolve().parent.parent
              / "mcp" / "mcp_server" / "server.py").read_text(encoding="utf-8")
    add_tool = source[source.index("def plan_add("):source.index("def plan_claim(")]
    assert "after: str | None = None" in add_tool
    assert "before: str | None = None" in add_tool
    assert "placed_for: str | None = None" in add_tool
    assert '"after": after, "before": before, "placed_for": placed_for' in add_tool
    assert "Placing is not reordering" in add_tool
    read_tool = source[source.index("def plan_read("):source.index("def plan_add(")]
    assert "caveat" in read_tool


def test_the_mcp_plan_tools_restate_neither_the_ttl_nor_the_limit():
    """`mcp[cli]` is the MCP package's own dependency and is not installed here,
    so the tool module cannot be imported — but the two things that must not
    drift are readable without importing it: a hardcoded default TTL changes
    behaviour the day the board's changes, and a `limit` the tool cannot pass is
    a cap every MCP caller hits without being told."""
    source = (Path(__file__).resolve().parent.parent
              / "mcp" / "mcp_server" / "server.py").read_text(encoding="utf-8")
    claim = source[source.index("def plan_claim("):source.index("def plan_release(")]
    assert "ttl: int | None = None" in claim and "ttl: int = 3600" not in claim
    read_tool = source[source.index("def plan_read("):source.index("def plan_add(")]
    assert "limit" in read_tool
