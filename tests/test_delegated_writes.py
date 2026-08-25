"""A delegated agent may reorder the plan — and is still not a person (#478).

`plan_order` computed the order the facts imply and could not apply it, so "sort
the plan for me" ended in a list somebody re-enacted by eye. The first design gave
an agent a person's session cookie, which made it `human/rich`: every human-only
write opened at once and an agent-applied order was indistinguishable from a typed
one. This is the replacement — a narrow, per-machine credential authorising a
NAMED set of writes, with the caller keeping its own name.

Three properties carry the whole argument and each has a test below:

  * a person still gets through unchanged — `/plan/reorder` is what the browser
    board's ▲▼ call, and they must keep working;
  * the secret is keyed to the machine the BEARER named, so one machine cannot
    spend another's;
  * the endpoints this does NOT cover stay human-only, which is the entire reason
    for a second credential rather than lending out the first.
"""

from __future__ import annotations

import pytest

from .conftest import DESKTOP, LAPTOP, LAPTOP_ELEVATED, PINNED_SETTINGS, SERVER

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _leave_the_plan_as_we_found_it(client):
    """Close this module's items when each test ends.

    Not tidiness. `qbdata.PLAN_LIMIT` caps a fleet-wide `GET /plan` at 200 rows —
    *"a plan is tens of rows by design; this is a backstop"* — and
    `test_plans.py::test_the_DASHBOARD_reads_a_co_tenants_held_plan_as_held` walks
    that read looking for its own row. A module that leaves a dozen open items
    behind pushes somebody else's row off the page, and the failure lands over
    there with nothing pointing back here.
    """
    yield
    r = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    for i in r.json().get("items", []):
        if i["repo"] == REPO and i["state"] == "open":
            await client.post("/plan/item/done", json={"item_id": i["item_id"]},
                              headers=LAPTOP)

HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
REPO = "acme/one"


async def seed(client, n=2) -> list[str]:
    ids = []
    for i in range(n):
        r = await client.post("/plan/item", json={"title": f"item {i}", "repo": REPO},
                              headers=LAPTOP)
        assert r.status_code in (200, 201), r.text
        ids.append(r.json()["item_id"])
    return ids


async def order_is(client, order, headers):
    return await client.post("/plan/reorder", json={"repo": REPO, "order": order},
                             headers=headers)


async def sources(client) -> dict[str, str]:
    r = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    return {i["item_id"]: i["rank_source"] for i in r.json()["items"]}


async def test_a_delegated_agent_may_reorder(client):
    a, b = await seed(client)
    r = await order_is(client, [b, a], LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text
    assert r.json()["reordered"] == 2


async def test_what_it_writes_is_derived_and_not_ordered(client):
    """The visible half of the whole change. `ordered` means a person chose this
    position; writing it for an agent would make the two indistinguishable in the
    one field a client can read, which is #183's substitution one layer down."""
    a, b = await seed(client)
    await order_is(client, [b, a], LAPTOP_ELEVATED)
    assert (await sources(client))[b] == "derived"


async def test_a_person_still_writes_ordered(client):
    """Unchanged, and it must be: the browser board's arrows call this endpoint."""
    a, b = await seed(client)
    r = await order_is(client, [b, a], HUMAN)
    assert r.status_code == 200, r.text
    assert (await sources(client))[b] == "ordered"


async def test_the_response_says_who_did_it(client):
    a, b = await seed(client)
    r = await order_is(client, [b, a], LAPTOP_ELEVATED)
    assert r.json()["by"].split("/")[0] == "laptop", r.json()["by"]
    assert not r.json()["by"].startswith("human/"), "a delegated agent is not a person"


async def test_a_bearer_alone_is_refused(client):
    """The gate is not weakened for agents in general — only for one holding the
    extra credential.

    No items seeded, here or in the refusals below: `Depends(delegated)` runs
    before the body, so the order is never read. Seeding for a refusal adds rows
    to a list `qbdata.PLAN_LIMIT` caps at 200 fleet-wide, to prove nothing.
    """
    r = await order_is(client, ["not-read"], LAPTOP)
    assert r.status_code == 403
    assert "X-Agent-Elevated" in r.text


async def test_one_machine_cannot_spend_anothers_secret(client):
    """The reason the secret is a map and not a single value: a leak is revoked by
    editing one line, and cannot be replayed from anywhere else in the fleet."""
    r = await order_is(client, ["not-read"],
                       {**SERVER, "X-Agent-Elevated": "not-a-secret-laptop"})
    assert r.status_code == 403
    assert "per machine" in r.text


async def test_a_machine_with_no_secret_configured_is_refused(client):
    """Unprovisioned is closed, not open — the rule `_edge_asserted` already keeps."""
    r = await order_is(client, ["not-read"], {**DESKTOP, "X-Agent-Elevated": "anything"})
    assert r.status_code == 403


async def test_an_unauthenticated_caller_gets_401_not_403(client):
    r = await client.post("/plan/reorder", json={"repo": REPO, "order": ["not-read"]})
    assert r.status_code == 401


async def test_the_credential_does_not_open_dials(client):
    """The whole point of a second credential rather than lending out the first:
    what it does NOT cover is where the blast radius stops. A dial is a judgement
    about what a review is worth and stays a person's."""
    r = await client.post("/dials", json={"dial": "review_panel.max_rounds",
                                          "value": 9, "reason": "test"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 403


async def test_the_credential_does_not_let_an_agent_declare_a_scope(client):
    """The third human-only write the delegation deliberately does not cover."""
    r = await client.post("/plan/scope",
                          json={"scope": "project:delegated-probe", "label": "probe"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 403, r.text


async def test_a_delegated_agent_may_correct_an_items_note(client):
    """The other half: an agent writes an item's reasoning, the issue moves on, and
    correcting its own note overrides nobody's judgement."""
    (a,) = await seed(client, 1)
    r = await client.post("/plan/item/update",
                          json={"item_id": a, "note": "corrected"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text


async def test_order_trust_counts_derived_apart_from_unchosen(client):
    """`trusted` deliberately does NOT go false on a derived order — the
    `picked-up` migration settled that a new source must not make the plan read as
    less trustworthy for the sole reason that agents were working."""
    a, b = await seed(client)
    await order_is(client, [b, a], LAPTOP_ELEVATED)
    r = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    body = r.json()
    trust = body["order_trust"]
    assert trust["derived"] >= 2
    assert trust["by_source"].get("derived") >= 2
    # The property that matters: a derived row is NOT counted as one nobody chose.
    # Asserted against this read's own rows rather than a constant, because the
    # suite shares a scope and earlier tests leave appended items behind.
    appended = sum(1 for i in body["items"] if i["rank_source"] == "appended")
    assert trust["unchosen"] == appended
    assert "instruction" in (trust["derived_hint"] or "")
