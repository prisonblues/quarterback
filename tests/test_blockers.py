"""Raising a question a human owes an answer to, and recording the answer — #328.

The gap this closes was measured rather than argued: `counts.blocked` read **0
across 20 open items** on a plan where three carried a blocker written as English
inside `note` — "RANK IS WRONG AND A HUMAN MUST FIX IT" among them. Countable by
nobody, and picked up by `next` like ordinary work.

The one interesting rule here is who may close one, and it is
`exempt_item`'s shape: one endpoint, and the caller's credential decides which act
happened. A person ANSWERS; an agent may only WITHDRAW, and only its own.
"""

from __future__ import annotations

import pytest

from .conftest import LAPTOP, PINNED_SETTINGS, SERVER

pytestmark = pytest.mark.anyio

HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
REPO = "acme/blockers"

#: Plan items this module created, closed on the way out. Not tidiness: #486 —
#: `qbdata.PLAN_LIMIT` caps a fleet-wide `GET /plan` at 200 rows, and
#: `test_plans.py::test_the_DASHBOARD_reads_a_co_tenants_held_plan_as_held` walks
#: that read looking for its own row. A module that leaves items behind pushes
#: somebody else's row off the page, and the failure lands over there with
#: nothing pointing back here. It caught this file on its first full run.
MINE: set[str] = set()


@pytest.fixture(autouse=True)
async def _leave_the_plan_as_we_found_it(client):
    MINE.clear()
    yield
    for item_id in sorted(MINE):
        await client.post("/plan/item/done", json={"item_id": item_id},
                          headers=LAPTOP)
    MINE.clear()


async def raise_one(client, headers=LAPTOP, **kw):
    body = {"subject_kind": "item", "subject_value": "i1", "kind": "decision",
            "question": "which of these?", "repo": REPO}
    body.update(kw)
    return await client.post("/blockers", json=body, headers=headers)


async def test_a_blocker_is_raised_and_comes_back_open(client):
    r = await raise_one(client)
    assert r.status_code == 200, r.text
    b = r.json()["blocker"]
    assert r.json()["raised"] is True
    assert b["question"] == "which of these?"
    assert b["resolved_at"] is None and b["resolution"] is None
    assert b["raised_by"].split("/")[0] == "laptop"


async def test_re_raising_the_same_question_is_a_no_op_not_an_error(client):
    """A loop that asks every run must not fill the table — and must not have to
    check first either, because two loops asking in the same second would both
    pass a check and both insert."""
    first = (await raise_one(client, subject_value="i-dup")).json()
    again = await raise_one(client, subject_value="i-dup", question="which of these?")
    assert again.status_code == 200, again.text
    assert again.json()["raised"] is False
    assert again.json()["blocker"]["id"] == first["blocker"]["id"]


async def test_the_same_subject_can_carry_a_second_question_of_another_class(client):
    """One live question per (subject, class), not per subject: "which approach"
    and "does it look right" are different questions about one thing."""
    await raise_one(client, subject_value="i-two", kind="decision")
    r = await raise_one(client, subject_value="i-two", kind="ui", question="right shape?")
    assert r.json()["raised"] is True


async def test_an_unknown_class_is_refused_and_names_the_vocabulary(client):
    r = await raise_one(client, kind="urgent")
    assert r.status_code == 422
    assert "decision" in r.text and "other" in r.text


async def test_authorisation_is_not_a_class(client):
    """Six, not seven. #328 proposed `authorisation`; `app/needs_human.py`'s own
    growth rule is that a word is earned by turning up under `other`, and nothing
    has ever been filed under it. Rich, 2026-08-26: agents have wide autonomy for
    gh actions, so the evidence is unlikely to arrive."""
    r = await raise_one(client, kind="authorisation")
    assert r.status_code == 422


async def test_a_question_is_required_to_be_a_sentence(client):
    r = await raise_one(client, question="")
    assert r.status_code == 422


# ---------------------------------------------------- who may close one


async def test_a_person_answers_and_the_resolution_is_stored(client):
    b = (await raise_one(client, subject_value="i-ans")).json()["blocker"]
    r = await client.post("/blockers/resolve",
                          json={"blocker_id": b["id"], "resolution": "go with A"},
                          headers=HUMAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["answered"] is True and out["withdrawn"] is False
    assert out["blocker"]["resolution"] == "go with A"
    assert out["blocker"]["resolved_by"].startswith("human/")
    assert out["blocker"]["answered_by_a_person"] is True


async def test_an_agent_may_withdraw_a_question_it_raised(client):
    """A loop that finds the answer two minutes after asking should take it out of
    a person's queue. Recorded as a withdrawal, not an answer."""
    b = (await raise_one(client, subject_value="i-wd")).json()["blocker"]
    r = await client.post("/blockers/resolve",
                          json={"blocker_id": b["id"], "resolution": "found it in the docs"},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["withdrawn"] is True and out["answered"] is False
    assert out["blocker"]["answered_by_a_person"] is False, (
        "a withdrawal must not read as a person's answer")


async def test_an_agent_may_not_close_somebody_elses_question(client):
    """Withdrawing somebody else's question is answering it — which is the act
    this table exists to route to a person."""
    b = (await raise_one(client, subject_value="i-other", headers=LAPTOP)).json()["blocker"]
    r = await client.post("/blockers/resolve",
                          json={"blocker_id": b["id"], "resolution": "I decided"},
                          headers=SERVER)
    assert r.status_code == 403, r.text
    assert "person" in r.text.lower()


async def test_a_resolution_cannot_be_overwritten(client):
    """The resolution is the record the next agent reads; a second one would
    silently replace a human's words."""
    b = (await raise_one(client, subject_value="i-once")).json()["blocker"]
    await client.post("/blockers/resolve",
                      json={"blocker_id": b["id"], "resolution": "A"}, headers=HUMAN)
    again = await client.post("/blockers/resolve",
                              json={"blocker_id": b["id"], "resolution": "actually B"},
                              headers=HUMAN)
    assert again.status_code == 409
    assert "already resolved" in again.text


async def test_answering_frees_the_subject_for_a_new_question(client):
    """The uniqueness index is on OPEN rows: an answered question must not be the
    thing that stops the same one being asked again later."""
    b = (await raise_one(client, subject_value="i-again")).json()["blocker"]
    await client.post("/blockers/resolve",
                      json={"blocker_id": b["id"], "resolution": "A for now"},
                      headers=HUMAN)
    r = await raise_one(client, subject_value="i-again")
    assert r.json()["raised"] is True, "an answered question blocked a new one"


# ---------------------------------------------------- the queue


async def test_the_queue_is_oldest_first_and_grouped_by_class(client):
    for n, k in (("q1", "decision"), ("q2", "ui"), ("q3", "ui")):
        await raise_one(client, subject_value=n, kind=k)
    r = await client.get("/blockers", params={"repo": REPO}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ordering"] == "oldest first"
    raised = [b["raised_at"] for b in out["blockers"]]
    assert raised == sorted(raised), "the queue was not oldest first"
    assert out["by_class"]["ui"] >= 2


async def test_the_queue_can_be_asked_what_is_mine(client):
    """The `N waiting on you` chip must not claim unowned work is yours, so an
    owner filter has to exist and an unowned blocker must not match it."""
    await raise_one(client, subject_value="i-mine", owner="human/rich")
    await raise_one(client, subject_value="i-anyone")
    r = await client.get("/blockers", params={"repo": REPO, "owner": "human/rich"},
                         headers=LAPTOP)
    vals = [b["subject"]["value"] for b in r.json()["blockers"]]
    assert "i-mine" in vals and "i-anyone" not in vals


async def test_resolved_blockers_are_out_of_the_queue_but_still_readable(client):
    b = (await raise_one(client, subject_value="i-hist")).json()["blocker"]
    await client.post("/blockers/resolve",
                      json={"blocker_id": b["id"], "resolution": "settled"},
                      headers=HUMAN)
    open_q = await client.get("/blockers", params={"repo": REPO}, headers=LAPTOP)
    assert b["id"] not in [x["id"] for x in open_q.json()["blockers"]]
    all_q = await client.get("/blockers", params={"repo": REPO, "open": "false"},
                             headers=LAPTOP)
    got = next(x for x in all_q.json()["blockers"] if x["id"] == b["id"])
    assert got["resolution"] == "settled", "the answer must survive as the record"


# ------------------------------------- what it changes about the plan (#328)


async def add_item(client, title, repo=REPO):
    r = await client.post("/plan/item", json={"title": title, "repo": repo},
                          headers=LAPTOP)
    assert r.status_code in (200, 201), r.text
    MINE.add(r.json()["item_id"])
    return r.json()["item_id"]


async def plan(client, repo=REPO):
    r = await client.get("/plan", params={"repo": repo}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    return r.json()


async def test_next_skips_an_item_waiting_on_a_human(client):
    """The whole point, and the failure that was measured: an item parked on a
    decision read as ordinary open work and was handed to the next agent that
    asked. `counts.blocked` was 0 across 20 items while three of them carried a
    blocker written as prose in `note`."""
    repo = "acme/next-blocked"
    first = await add_item(client, "parked on a decision", repo=repo)
    second = await add_item(client, "actually free", repo=repo)

    before = await plan(client, repo)
    assert before["next"]["item_id"] == first, "precondition: it was next"

    r = await client.post("/blockers", json={
        "subject_kind": "item", "subject_value": first, "kind": "decision",
        "question": "which approach?", "repo": repo}, headers=LAPTOP)
    assert r.status_code == 200, r.text

    after = await plan(client, repo)
    assert after["next"]["item_id"] == second, "next handed out a blocked item"


async def test_answering_it_puts_the_item_back_in_the_queue(client):
    """A blocker is a state, not a tombstone — the resolution is what releases it,
    which is the half a `stuck` post could never do."""
    repo = "acme/next-freed"
    only = await add_item(client, "waits then proceeds", repo=repo)
    b = (await client.post("/blockers", json={
        "subject_kind": "item", "subject_value": only, "kind": "taste",
        "question": "right name?", "repo": repo}, headers=LAPTOP)).json()["blocker"]
    assert (await plan(client, repo))["next"] is None

    await client.post("/blockers/resolve",
                      json={"blocker_id": b["id"], "resolution": "call it a drain"},
                      headers=HUMAN)
    assert (await plan(client, repo))["next"]["item_id"] == only


async def test_the_two_kinds_of_blocked_are_counted_apart(client):
    """One waits on work finishing, the other on somebody answering, and the
    remedy differs — so `blocked` keeps its old meaning and the new kind gets its
    own number rather than being folded in."""
    repo = "acme/two-kinds"
    a = await add_item(client, "the dependency", repo=repo)
    b_item = await add_item(client, "waits on the item", repo=repo)
    c = await add_item(client, "waits on a person", repo=repo)
    await client.post("/plan/item/depends",
                      json={"item_id": b_item, "depends_on": [a]}, headers=LAPTOP)
    await client.post("/blockers", json={
        "subject_kind": "item", "subject_value": c, "kind": "ui",
        "question": "does it look right?", "repo": repo}, headers=LAPTOP)

    counts = (await plan(client, repo))["counts"]
    assert counts["blocked"] == 1, counts
    assert counts["waiting_on_a_human"] == 1, counts


async def test_the_item_says_what_it_is_waiting_for_and_for_how_long(client):
    """The three questions worth asking about a blocker are all state questions —
    how many, how old, whose — and none is answerable over a post stream."""
    repo = "acme/says-why"
    only = await add_item(client, "parked", repo=repo)
    await client.post("/blockers", json={
        "subject_kind": "item", "subject_value": only, "kind": "decision",
        "question": "A or B?", "owner": "human/rich", "repo": repo}, headers=LAPTOP)

    row = next(i for i in (await plan(client, repo))["items"] if i["item_id"] == only)
    (w,) = row["waiting_on_a_human"]
    assert w["class"] == "decision"
    assert w["question"] == "A or B?"
    assert w["owner"] == "human/rich"
    assert w["idle_days"] is not None, "age is the signal nobody has to maintain"
    assert row["blocked_by"] == [], "a human blocker must not masquerade as an item edge"
