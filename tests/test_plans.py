"""#172: a plan is a row — submitted atomically, claimable as a unit.

`plan_items.phase` was a free-text string on an item, and it was the plan's own
copy of the defect this release is about: **a name composed by whoever typed it.**
Four consequences, all of them observed on this board:

* "stage 1" and "Stage 1" were two phases and nothing could tell.
* A phase had no state, so nothing could say it was finished.
* A phase could not be claimed. #172's one genuinely fuzzy race is two agents
  surveying the same vague problem at once — before any item exists to be exact
  about — and there was no object at that grain to hold.
* A plan arrived one `POST /plan/item` at a time, so an eight-item plan landed
  incrementally and a second agent could claim from a half-written one. The
  raider is not even wrong: what it read really was the plan at that moment.

So the properties under test are the four fixes, plus the two rules the plan
already had and must keep: a claim is never reimplemented (it is a
`resource_leases` row with a derived key), and only a human reorders.
"""

from __future__ import annotations

import uuid

from app.db import async_session
from app.models.plan import Plan

from .conftest import DESKTOP, LAPTOP

EDGE = {**LAPTOP, "Remote-User": "person", "X-Edge-Auth": "tok-edge"}


async def submit(client, repo: str, label: str, items: list[dict], headers=LAPTOP,
                 **over):
    return await client.post("/plan/submit", json={
        "repo": repo, "label": label, "items": items, **over}, headers=headers)


async def submitted(client, repo: str, label: str, items: list[dict], headers=LAPTOP,
                    **over) -> dict:
    r = await submit(client, repo, label, items, headers=headers, **over)
    assert r.status_code == 200, r.text
    return r.json()


def item(number: int, **over) -> dict:
    return {"title": f"#{number}", "ref_kind": "issue", "ref_value": str(number),
            **over}


async def read(client, repo: str, headers=LAPTOP, **params) -> dict:
    r = await client.get("/plan", params={"repo": repo, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------- submitted as a unit or not

async def test_a_whole_plan_lands_in_one_call_ordered_and_claimed(client):
    repo = "acme/submitone"
    out = await submitted(client, repo, "stage 1",
                          [item(1, note="first because its schema is the shared one"),
                           item(2), item(3)],
                          note="the migration, in the order it has to happen")
    assert out["label"] == "stage 1"
    assert [i["ref"]["value"] for i in out["items"]] == ["1", "2", "3"]
    assert [i["rank"] for i in out["items"]] == [1, 2, 3]
    # Claimed on the way out by default: the surveying agent wrote it, and the gap
    # between writing and holding is the gap a second agent raids.
    assert out["claim"] is not None
    assert out["claim"]["key"].startswith("plan:")
    assert out["items"][0]["plan"]["label"] == "stage 1"


async def test_nothing_is_written_when_one_line_collides(client):
    """All-or-nothing, and the refusal names WHICH line — a caller told only
    "something in there collides" has to bisect its own plan."""
    repo = "acme/submitatomic"
    first = await submitted(client, repo, "wave 1", [item(10)])
    r = await submit(client, repo, "wave 2", [item(11), item(10), item(12)])
    assert r.status_code == 409, r.text
    assert [c["ref"] for c in r.json()["detail"]["clashes"]] == ["issue 10"]

    plan = await read(client, repo)
    assert [i["ref"]["value"] for i in plan["items"]] == ["10"], \
        "part of the refused submission landed anyway"
    assert [p["label"] for p in plan["plans"]] == ["wave 1"]
    assert first["items"][0]["item_id"] == plan["items"][0]["item_id"]


async def test_a_submission_cannot_reference_one_issue_twice(client):
    """Caught before the index sees it, because the index would report it as
    "already in the plan" about a row this very request created — sending the
    caller to look for somebody else's item."""
    r = await submit(client, "acme/submitdupe", "dupes", [item(20), item(20)])
    assert r.status_code == 422, r.text
    assert "items 1 and 2" in r.json()["detail"]["error"]


async def test_a_plan_carries_its_own_dependency_graph(client):
    """`@1` means the first item of THIS submission. Without it a plan whose edges
    can only point at rows that already exist has to be written twice — which is
    the incremental submission this endpoint replaces."""
    repo = "acme/submitdeps"
    out = await submitted(client, repo, "layered",
                          [item(30), item(31, depends_on=["@1"]),
                           item(32, depends_on=["@2", "#30"])])
    by_ref = {i["ref"]["value"]: i for i in out["items"]}
    assert by_ref["30"]["depends_on"] == []
    assert by_ref["31"]["depends_on"] == [by_ref["30"]["item_id"]]
    assert set(by_ref["32"]["depends_on"]) == {by_ref["31"]["item_id"],
                                               by_ref["30"]["item_id"]}
    # And the plan reads truthfully: only the first is free.
    plan = await read(client, repo)
    assert plan["next"]["ref"]["value"] == "30"
    assert plan["counts"]["blocked"] == 2


async def test_a_circular_batch_edge_is_refused_before_anything_is_written(client):
    repo = "acme/submitcycle"
    r = await submit(client, repo, "ring",
                     [item(40, depends_on=["@2"]), item(41, depends_on=["@1"])])
    assert r.status_code == 422, r.text
    assert "circular" in r.json()["detail"]["error"]
    assert (await read(client, repo))["items"] == []


async def test_a_batch_reference_out_of_range_is_refused(client):
    r = await submit(client, "acme/submitrange", "oops", [item(50, depends_on=["@9"])])
    assert r.status_code == 422, r.text
    assert "not an item of this submission" in r.json()["detail"]["error"]


async def test_submitting_over_an_open_plan_is_refused_not_merged(client):
    """"Add to that one" and "submit a plan" are different intentions, and quietly
    merging them is how a raided plan looks like a successful submission."""
    repo = "acme/submittwice"
    first = await submitted(client, repo, "stage 1", [item(60)])
    again = await submit(client, repo, "Stage 1", [item(61)])
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["plan_id"] == first["plan_id"]


# ------------------------------------------------- one row per label, folded

async def test_two_spellings_of_one_label_are_one_plan(client):
    """The `phase` defect, as a test. "stage 1" and "Stage 1" were two phases and
    nothing could tell; now the case-folded unique index makes it one row."""
    repo = "acme/labelfold"
    await submitted(client, repo, "stage 1", [item(70)])
    added = await client.post("/plan/item", json={
        "title": "#71", "repo": repo, "ref_kind": "issue", "ref_value": "71",
        "plan": "STAGE 1"}, headers=LAPTOP)
    assert added.status_code == 200, added.text
    plan = await read(client, repo)
    assert len(plan["plans"]) == 1
    assert plan["plans"][0]["items"] == {"open": 2}
    assert {i["plan"]["plan_id"] for i in plan["items"]} == {plan["plans"][0]["plan_id"]}


async def test_an_unknown_label_on_plan_add_creates_the_plan(client):
    """Find-or-create, and not the strictness it looks like it is missing:
    refusing would make the commonest thing an agent does a two-call dance. The
    discipline #172 asks for is ONE row per label, which the index provides
    however the row was made."""
    repo = "acme/labelnew"
    added = await client.post("/plan/item", json={
        "title": "#80", "repo": repo, "ref_kind": "issue", "ref_value": "80",
        "plan": "improvised"}, headers=LAPTOP)
    assert added.status_code == 200, added.text
    assert added.json()["plan"]["label"] == "improvised"


async def test_a_human_moving_an_item_must_name_a_plan_that_exists(client):
    """The other direction, and the asymmetry is deliberate: an agent adding work
    is naming the plan it is working in, a human moving an item is making a
    decision about an object — and inventing one off a typo is how two spellings
    happened in the first place."""
    repo = "acme/labelmove"
    out = await submitted(client, repo, "here", [item(90)])
    item_id = out["items"][0]["item_id"]
    bad = await client.post("/plan/item/update",
                            json={"item_id": item_id, "plan": "nowhere"}, headers=EDGE)
    assert bad.status_code == 422, bad.text
    detached = await client.post("/plan/item/update",
                                json={"item_id": item_id, "plan": ""}, headers=EDGE)
    assert detached.status_code == 200, detached.text
    assert detached.json()["plan"] is None


# ------------------------------------------------------ claiming a whole plan

async def test_holding_a_plan_covers_its_items_for_everybody_else(client):
    """The one coarse grain in the system. Four agents at a vague problem: the one
    that wrote the plan says "all of this is mine" once, instead of holding twenty
    item claims — and everybody else's `next` skips past it."""
    repo = "acme/covered"
    out = await submitted(client, repo, "mine", [item(100), item(101)],
                          session="s-owner")
    assert out["claim"] is not None

    theirs = await read(client, repo, headers=DESKTOP)
    assert theirs["next"] is None, "a covered item was offered as free work"
    assert all(i["covered_by"]["holder"] == out["claim"]["holder"]
               for i in theirs["items"])
    assert theirs["counts"]["covered"] == 2
    # ...and it is NOT reported as an item claim: the item itself is genuinely
    # unclaimed, and saying otherwise would make releasing it a puzzling 404.
    assert all(i["claim"] is None for i in theirs["items"])


async def test_your_own_plan_claim_never_covers_anything_from_you(client):
    """It is what lets the holder work through its own plan item by item."""
    repo = "acme/coveredmine"
    await submitted(client, repo, "mine", [item(110)], session="s-owner")
    mine = await read(client, repo, headers=LAPTOP)
    assert mine["next"] is not None
    assert mine["items"][0]["covered_by"] is None


async def test_a_plan_claim_is_an_ordinary_claim_on_the_one_table(client):
    """No second implementation of "who has this right now" — the fourth feature
    to want one, and the third to be told no."""
    repo = "acme/planclaimrow"
    out = await submitted(client, repo, "rowcheck", [item(120)], session="s-row")
    key = out["claim"]["key"]
    listed = await client.get("/claims", params={"key": key}, headers=LAPTOP)
    assert [c["claim_id"] for c in listed.json()["claims"]] == [out["claim"]["claim_id"]]
    # And it answers the pickup question — under `unattributed`, because a plan
    # may span repos and attributing it to one would be a guess.
    held = await client.get("/claim/held", headers=LAPTOP)
    assert any(c["key"] == key for c in held.json()["unattributed"])


async def test_a_co_tenant_cannot_hold_a_plan_another_agent_holds(client):
    """Session-owned, like an item claim and for the same reason: a machine runs
    several agents on one token, and "two agents on one box both hold the plan" is
    the failure it exists to prevent."""
    repo = "acme/plancotenant"
    out = await submitted(client, repo, "exclusive", [item(130)], session="s-first")
    r = await client.post("/plan/claim",
                          json={"plan_id": out["plan_id"], "session": "s-second"},
                          headers=LAPTOP)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["session"] == "s-first"


async def test_releasing_a_plan_frees_its_items_for_the_next_agent(client):
    repo = "acme/planrelease"
    out = await submitted(client, repo, "handover", [item(140)], session="s-h")
    let_go = await client.post("/plan/release",
                               json={"plan_id": out["plan_id"], "session": "s-h"},
                               headers=LAPTOP)
    assert let_go.status_code == 200 and let_go.json()["released"] is True
    assert (await read(client, repo, headers=DESKTOP))["next"] is not None
    # Idempotent: holding nothing is a fine answer, not an error.
    again = await client.post("/plan/release",
                              json={"plan_id": out["plan_id"], "session": "s-h"},
                              headers=LAPTOP)
    assert again.status_code == 200 and again.json()["released"] is False


async def test_a_co_tenant_cannot_release_a_plan_it_does_not_hold(client):
    repo = "acme/planreleaseother"
    out = await submitted(client, repo, "notyours", [item(150)], session="s-owner")
    r = await client.post("/plan/release",
                          json={"plan_id": out["plan_id"], "session": "s-other"},
                          headers=LAPTOP)
    assert r.status_code == 403, r.text


async def test_submitting_without_claiming_leaves_the_plan_free(client):
    repo = "acme/plannoclaim"
    out = await submitted(client, repo, "unclaimed", [item(160)], claim=False)
    assert out["claim"] is None
    assert (await read(client, repo, headers=DESKTOP))["next"] is not None


# ----------------------------------------------------------- finishing a plan

async def test_a_plan_with_open_items_is_not_finishable_by_accident(client):
    """"Finished" and "six items outstanding" cannot both be true, and the plan is
    what the next agent reads."""
    repo = "acme/planopen"
    out = await submitted(client, repo, "halfway", [item(170), item(171)],
                          session="s-f")
    r = await client.post("/plan/done",
                          json={"plan_id": out["plan_id"], "session": "s-f"},
                          headers=LAPTOP)
    assert r.status_code == 409, r.text
    assert len(r.json()["detail"]["items"]) == 2

    forced = await client.post("/plan/done", json={
        "plan_id": out["plan_id"], "session": "s-f", "force": True,
        "note": "superseded by the epic"}, headers=LAPTOP)
    assert forced.status_code == 200, forced.text
    assert forced.json()["state"] == "done"
    assert len(forced.json()["items_left"]) == 2
    assert "superseded by the epic" in forced.json()["note"]


async def test_finishing_a_plan_releases_the_holders_own_claim(client):
    repo = "acme/plandone"
    out = await submitted(client, repo, "complete", [item(180)], session="s-d")
    done_item = await client.post("/plan/item/done",
                                  json={"item_id": out["items"][0]["item_id"],
                                        "session": "s-d"}, headers=LAPTOP)
    assert done_item.status_code == 200, done_item.text
    finished = await client.post("/plan/done",
                                 json={"plan_id": out["plan_id"], "session": "s-d"},
                                 headers=LAPTOP)
    assert finished.status_code == 200, finished.text
    assert finished.json()["claim"] is None
    listed = await client.get("/claims", params={"key": out["claim"]["key"]},
                              headers=LAPTOP)
    assert listed.json()["claims"] == [], "the plan's claim outlived the plan"


async def test_a_finished_label_is_free_for_the_next_plan(client):
    """The unique index covers OPEN plans only, so "stage 1" can happen twice —
    once per release — and the history of both survives."""
    repo = "acme/planrelabel"
    first = await submitted(client, repo, "stage 1", [item(190)], claim=False)
    await client.post("/plan/done", json={"plan_id": first["plan_id"], "force": True},
                      headers=LAPTOP)
    second = await submit(client, repo, "stage 1", [item(191)], claim=False)
    assert second.status_code == 200, second.text
    assert second.json()["plan_id"] != first["plan_id"]


async def test_a_dropped_plan_cannot_be_finished(client):
    """The mirror of the item rule: a human deciding this should not happen must
    not be routed around by an agent recording that it did."""
    repo = "acme/plandropped"
    out = await submitted(client, repo, "cancelled", [item(200)], claim=False)
    # There is no drop endpoint for a plan yet; force it through the model the way
    # a human's `update` would, so the refusal itself is what is under test.
    async with async_session() as s:
        row = await s.get(Plan, uuid.UUID(out["plan_id"]))
        row.state = "dropped"
        await s.commit()
    r = await client.post("/plan/done", json={"plan_id": out["plan_id"]},
                          headers=LAPTOP)
    assert r.status_code == 409, r.text
    assert "dropped this plan" in r.json()["detail"]["error"]


# --------------------------------------------------------------- reading them

async def test_plans_are_listed_with_who_holds_them_and_how_much_is_left(client):
    """The read an agent makes BEFORE it starts surveying. On every plan read too,
    not behind its own endpoint: an answer that needs a second call is an answer
    agents do not fetch, and #172's evidence is a fleet where nobody called the
    primitive at all."""
    repo = "acme/planslist"
    out = await submitted(client, repo, "surveyed", [item(210), item(211)],
                          session="s-s", note_on_claim="working out the order")
    r = await client.get("/plans", params={"repo": repo}, headers=DESKTOP)
    assert r.status_code == 200, r.text
    row = r.json()["plans"][0]
    assert row["label"] == "surveyed"
    assert row["items"] == {"open": 2}
    assert row["claim"]["note"] == "working out the order"
    assert row["covered_by"]["holder"] == out["claim"]["holder"]

    closed = await client.get("/plans", params={"repo": repo, "include_closed": True},
                              headers=DESKTOP)
    assert len(closed.json()["plans"]) == 1


async def test_reading_one_plan_narrows_the_items_and_the_counts(client):
    repo = "acme/plannarrow"
    await submitted(client, repo, "one", [item(220)], claim=False)
    await submitted(client, repo, "two", [item(221), item(222)], claim=False)
    narrowed = await read(client, repo, plan="two")
    assert [i["ref"]["value"] for i in narrowed["items"]] == ["221", "222"]
    assert narrowed["counts"]["open"] == 2
    assert narrowed["plan"]["label"] == "two"


async def test_reading_a_plan_that_does_not_exist_says_so(client):
    r = await client.get("/plan", params={"repo": "acme/plannone", "plan": "ghost"},
                         headers=LAPTOP)
    assert r.status_code == 422, r.text
    assert "no plan called" in r.json()["detail"]["error"]


async def test_a_narrowed_read_and_the_plans_list_agree_about_the_holder(client):
    """They did not. `plan` was rendered on its own and came back `claim: null`
    while the `plans` row beside it showed the live claim — the same subsystem
    answering one question two ways, which is the defect #172 is about, inside the
    endpoint that fixes it."""
    repo = "acme/planagree"
    out = await submitted(client, repo, "held", [item(230)], session="s-a")
    narrowed = await read(client, repo, plan="held")
    assert narrowed["plan"]["claim"] is not None
    assert narrowed["plan"]["claim"]["claim_id"] == out["claim"]["claim_id"]
    assert narrowed["plan"] == next(p for p in narrowed["plans"]
                                    if p["plan_id"] == out["plan_id"])


async def test_a_cycle_made_of_ISSUE_refs_inside_one_submission_is_refused(client):
    """The `@n` graph is checked before anything is written, but an item of the same
    submission is also reachable by its issue ref once it exists — so a ring can be
    written in the spelling the batch check does not read. It is caught by the plan
    router's own cycle guard as the second edge lands, and the submission rolls back
    whole."""
    repo = "acme/planrefcycle"
    r = await submit(client, repo, "ring",
                     [item(240, depends_on=["#241"]), item(241, depends_on=["#240"])])
    assert r.status_code == 422, r.text
    assert "circular" in r.text
    assert (await read(client, repo))["items"] == [], \
        "half the ring landed — a refused submission has to leave nothing"


async def test_a_bare_repo_name_cannot_be_read_as_a_scope(client):
    """The scope is half of every item's claim key now, so `quarterback` beside
    `prisonblues/quarterback` would key one issue two ways. Refused on the read path
    as well as the write path: answering about a scope nobody else spells that way
    is how a caller concludes work is free."""
    r = await client.get("/plan", params={"repo": "quarterback"}, headers=LAPTOP)
    assert r.status_code == 422, r.text
    assert "owner/name" in r.text


# ------------------------------------- what a plan claim actually stops (review)

async def test_holding_a_plan_REFUSES_an_item_claim_from_somebody_else(client):
    """A claim blocks. It is not a note to read past.

    Reporting the plan hold on the read path and then letting `plan/item/claim`
    take the item anyway is exactly the state #172 is about: a record everybody
    can see and nothing honours."""
    repo = "acme/planblocks"
    out = await submitted(client, repo, "mine", [item(250)], session="s-owner")
    r = await client.post("/plan/item/claim",
                          json={"item_id": out["items"][0]["item_id"],
                                "session": "s-raider"}, headers=DESKTOP)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["covered_by"]["label"] == "mine"
    assert r.json()["detail"]["covered_by"]["session"] == "s-owner"


async def test_force_takes_one_item_out_of_somebody_elses_plan_deliberately(client):
    """The plan holder may genuinely be sharing the work — but then "I know
    somebody holds the plan" is on the record rather than assumed."""
    repo = "acme/planforced"
    out = await submitted(client, repo, "mine", [item(251)], session="s-owner")
    r = await client.post("/plan/item/claim",
                          json={"item_id": out["items"][0]["item_id"],
                                "session": "s-raider", "force": True}, headers=DESKTOP)
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is True


async def test_the_plan_holder_may_still_claim_its_own_items(client):
    """The point of holding a plan is to work through it."""
    repo = "acme/planownitems"
    out = await submitted(client, repo, "mine", [item(252)], session="s-owner")
    r = await client.post("/plan/item/claim",
                          json={"item_id": out["items"][0]["item_id"],
                                "session": "s-owner"}, headers=LAPTOP)
    assert r.status_code == 200, r.text


async def test_a_CO_TENANT_sees_a_held_plan_as_somebody_elses_when_it_says_who_it_is(client):
    """#142's rule on the read path: a machine runs several agents on one token, so
    answering by machine alone told a co-tenant its neighbour's held plan was free.
    The machine is still the fallback — `GET /plan` authorises with `reader`, which
    knows nothing finer — so a caller that sends no session gets the coarser answer
    rather than a wrong one."""
    repo = "acme/plancotenantread"
    await submitted(client, repo, "theirs", [item(253)], session="s-first")
    # Same token (same machine), a different session: not mine.
    theirs = await read(client, repo, session="s-second")
    assert theirs["items"][0]["covered_by"] is not None
    assert theirs["next"] is None
    # ...and the holder itself still sees its own plan as its own.
    mine = await read(client, repo, session="s-first")
    assert mine["items"][0]["covered_by"] is None


async def test_an_item_cannot_be_moved_into_a_CLOSED_plan(client):
    """A plan named by id used to skip every check the label lookup makes, so an
    item could be moved into a finished list — invisible to the plan it left and
    inert in the one it joined."""
    repo = "acme/planclosedmove"
    closed = await submitted(client, repo, "over", [item(260)], claim=False)
    await client.post("/plan/done", json={"plan_id": closed["plan_id"], "force": True},
                      headers=LAPTOP)
    loose = await client.post("/plan/item", json={
        "title": "loose", "repo": repo, "ref_kind": "issue", "ref_value": "261"},
        headers=LAPTOP)
    r = await client.post("/plan/item/update",
                          json={"item_id": loose.json()["item_id"],
                                "plan": closed["plan_id"]}, headers=EDGE)
    assert r.status_code == 422, r.text


async def test_an_item_cannot_be_moved_into_another_repos_plan(client):
    """Across repos it would put a row in a list nobody reading that repo can see.
    A FLEET plan is reachable from every scope, because that is what a NULL scope is."""
    theirs = await submitted(client, "acme/planotherrepo", "elsewhere", [item(270)],
                             claim=False)
    mine = await client.post("/plan/item", json={
        "title": "here", "repo": "acme/planmyrepo", "ref_kind": "issue",
        "ref_value": "271"}, headers=LAPTOP)
    r = await client.post("/plan/item/update",
                          json={"item_id": mine.json()["item_id"],
                                "plan": theirs["plan_id"]}, headers=EDGE)
    assert r.status_code == 422, r.text

    fleet = await submit(client, None, "fleetwide", [item(272)], claim=False)
    assert fleet.status_code == 200, fleet.text
    ok = await client.post("/plan/item/update",
                           json={"item_id": mine.json()["item_id"],
                                 "plan": fleet.json()["plan_id"]}, headers=EDGE)
    assert ok.status_code == 200, ok.text
    assert ok.json()["plan"]["label"] == "fleetwide"


async def test_a_closed_plan_can_still_be_READ_by_id(client):
    """History is readable; only the writes require an open plan. `include_done` is
    the whole reason a closed plan needs to be nameable at all."""
    repo = "acme/planreadclosed"
    out = await submitted(client, repo, "finished", [item(280)], claim=False)
    await client.post("/plan/item/done",
                      json={"item_id": out["items"][0]["item_id"]}, headers=LAPTOP)
    await client.post("/plan/done", json={"plan_id": out["plan_id"]}, headers=LAPTOP)
    r = await client.get("/plan", params={"repo": repo, "plan": out["plan_id"],
                                          "include_done": True}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["state"] == "done"
    assert len(r.json()["items"]) == 1


async def test_a_plan_taken_DURING_an_item_claim_takes_the_item_claim_back(client):
    """The covering check and the claim are two statements, and `acquire` cannot be
    held inside a lock — it commits, which is where its atomicity comes from. So the
    check is made again afterwards, because "both claims live" is two agents each
    correctly believing the work is theirs, which is the one outcome a plan claim
    exists to prevent.

    Driven by claiming the plan between the two, which is what a concurrent request
    would do."""
    repo = "acme/planrace"
    out = await submitted(client, repo, "raced", [item(290)], claim=False)
    item_id = out["items"][0]["item_id"]

    # The window: the plan is free when `claim_item` looks, and held by the time it
    # re-reads. Simulated by taking the plan through the API from the other machine
    # while patching the first check to see a free plan.
    import app.api.plan as plan_api
    real = plan_api._covering_claim
    seen = {"n": 0}

    async def once_free(*a, **kw):
        seen["n"] += 1
        if seen["n"] == 1:
            await client.post("/plan/claim",
                              json={"plan_id": out["plan_id"], "session": "s-winner"},
                              headers=DESKTOP)
            return None
        return await real(*a, **kw)

    plan_api._covering_claim = once_free
    try:
        r = await client.post("/plan/item/claim",
                              json={"item_id": item_id, "session": "s-loser"},
                              headers=LAPTOP)
    finally:
        plan_api._covering_claim = real
    assert seen["n"] == 2, "the check was not made again after the claim"
    assert r.status_code == 409, r.text
    assert "took the whole plan while you were claiming" in r.json()["detail"]["error"]

    # ...and the item claim it briefly held was handed back, not left live.
    plan = await read(client, repo, headers=DESKTOP, session="s-winner")
    assert plan["items"][0]["claim"] is None, "two claims on one piece of work survived"


async def test_the_refusal_says_why_your_plan_read_disagreed(client):
    """`GET /plan` authorises with `reader`, which resolves a bearer token to a
    machine and knows nothing finer — so a caller that sends no session is answered
    by machine and a co-tenant's hold looks like its own. The write is exact and the
    read is coarse, and the refusal is where that asymmetry has to be explained,
    because it is the only place a caller meets it."""
    repo = "acme/planasym"
    await submitted(client, repo, "theirs", [item(291)], session="s-first")
    coarse = await read(client, repo)                     # no session: looks free
    assert coarse["items"][0]["covered_by"] is None
    r = await client.post("/plan/item/claim",
                          json={"item_id": coarse["items"][0]["item_id"],
                                "session": "s-second"}, headers=LAPTOP)
    assert r.status_code == 409, r.text
    assert "did not send `session` on GET /plan" in r.json()["detail"]["hint"]
