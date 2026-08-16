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
* **Only a human reorders.** Agents add, claim, record what they observe and
  complete; the sequence stays the human's, which is what stops it thrashing.
* **It never decides an item is done** — `done` records that the issue closed.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app.api.plan import STALE_DAYS, _item_view
from app.models.plan_item import PlanItem

from .conftest import DESKTOP, LAPTOP, SERVER

HUMAN = {"Remote-User": "rich"}


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
    assert plan["counts"] == {"open": 2, "claimed": 0, "blocked": 0, "stale": 0,
                              "done": 0, "dropped": 0}


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
    the next claimant's own request sweeps the key."""
    repo = "acme/deadagent"
    item = await issue(client, repo, 9)
    await take(client, item["item_id"], ttl=1, session="s-dies")
    await asyncio.sleep(1.1)

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

    r = await client.post("/plan/item/release", json={"item_id": item["item_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200 and r.json()["released"] is True
    assert r.json()["claim"] is None

    again = await client.post("/plan/item/release", json={"item_id": item["item_id"]},
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


async def test_a_phase_filter_is_applied_before_the_limit(client):
    """Filtering the returned page instead of the query dropped every match past
    the first `limit` rows — and with it `next`, which then read as "nothing to
    do in this phase" while the phase was full of work."""
    repo = "acme/phaselimit"
    await issue(client, repo, 200, phase="stage 1")
    second = await issue(client, repo, 201, phase="stage 2")

    plan = await read(client, repo, phase="stage 2", limit=1)
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
                          json={"item_id": item["item_id"], "note": "landed in PR #143"},
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
    assert r.json()["claim_left"] is True
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
