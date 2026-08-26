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


#: Every item this module created, so the teardown closes those and only those.
MINE: set[str] = set()


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
    MINE.clear()
    yield
    # Only this module's own rows. Closing every open item in the scope would
    # mutate rows another test created and is still using — `acme/one` is shared,
    # and a teardown that tidies somebody else's data is a worse bug than the
    # PLAN_LIMIT pressure it was written to relieve (#486).
    for item_id in sorted(MINE):
        await client.post("/plan/item/done", json={"item_id": item_id},
                          headers=LAPTOP)
    MINE.clear()

HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
REPO = "acme/one"


async def seed(client, n=2) -> list[str]:
    ids = []
    for i in range(n):
        r = await client.post("/plan/item", json={"title": f"item {i}", "repo": REPO},
                              headers=LAPTOP)
        assert r.status_code in (200, 201), r.text
        ids.append(r.json()["item_id"])
        MINE.add(ids[-1])
    return ids


async def order_is(client, order, headers):
    return await client.post("/plan/reorder", json={"repo": REPO, "order": order},
                             headers=headers)


async def sources(client) -> dict[str, str]:
    r = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    return {i["item_id"]: i["rank_source"] for i in r.json()["items"]}


async def ranks(client) -> dict[str, int]:
    r = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    return {i["item_id"]: i["rank"] for i in r.json()["items"]}


async def test_a_delegated_agent_may_reorder(client):
    """Reads the ranks back and compares them to the sequence asked for. A count
    alone passes for an implementation that returned the right number and applied
    the wrong permutation — and `reordered == 2` as a literal is brittle besides,
    since this scope is shared."""
    a, b = await seed(client)
    r = await order_is(client, [b, a], LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text
    assert r.json()["reordered"] == 2
    got = await ranks(client)
    assert got[b] < got[a], "the requested sequence was not applied"


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
    # The decision this migration argued for, asserted rather than implied: a
    # derived row must not flip `trusted`. Conditioned on there being no appended
    # rows, since those legitimately do.
    if appended == 0:
        assert trust["trusted"] is True, "a derived order flipped `trusted`"
    assert "instruction" in (trust["derived_hint"] or "")


# ------------------------------- what a delegated agent may NOT decide (#335)


async def test_a_delegated_agent_cannot_exempt_its_own_pr_through_a_note(client):
    """#335, reopened and closed again. `_refuse_agent_exemption`'s docstring names
    the two paths that may set the marker and says both take `app.auth.human` —
    `POST /plan/item/update` is one of them, so widening its gate reopened the hole
    through a door #335's own fix depends on.

    Measured before the guard existed: this exact call returned 200 and the item
    came back `review.exempt: true`. That is the authority `exempt_item` withholds
    by downgrading an agent's grant to a request, taken by a longer route.
    """
    r = await client.post("/plan/item",
                          json={"title": "my own pr", "repo": REPO,
                                "ref_kind": "pr", "ref_value": "9001"},
                          headers=LAPTOP)
    item = r.json()["item_id"]
    MINE.add(item)

    r = await client.post("/plan/item/update",
                          json={"item_id": item, "note": "review: exempt — trivial"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 403, r.text
    # And the refusal routes the agent to the thing it MAY do, rather than just
    # saying no — `exempt` records a request and leaves the PR in the queue.
    assert "/plan/item/exempt" in r.text

    q = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    row = next(i for i in q.json()["items"] if i["item_id"] == item)
    assert row["review"]["exempt"] is False, "a delegated agent exempted its own PR"


async def test_a_person_may_still_set_the_exemption_marker_here(client):
    """The guard refuses the ACT for an agent, not the endpoint for everyone —
    this is one of the two paths #335 deliberately left open to a person."""
    r = await client.post("/plan/item",
                          json={"title": "a pr", "repo": REPO,
                                "ref_kind": "pr", "ref_value": "9002"},
                          headers=LAPTOP)
    item = r.json()["item_id"]
    MINE.add(item)
    r = await client.post("/plan/item/update",
                          json={"item_id": item, "note": "review: exempt — release chore"},
                          headers=HUMAN)
    assert r.status_code == 200, r.text


async def test_a_delegated_agent_cannot_drop_an_item(client):
    """"a person decided it should not" is the endpoint's own description of the
    act. An agent deciding that about work it might be the one avoiding is the
    same self-approval shape one field over — and it reaches `live_claim`."""
    (a,) = await seed(client, 1)
    r = await client.post("/plan/item/update",
                          json={"item_id": a, "state": "dropped"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 403, r.text
    assert "person" in r.text.lower()


async def test_a_delegated_note_update_is_still_one_call(client):
    """The guard must not cost the legitimate case anything — re-reasoning an item
    is what the credential is FOR."""
    (a,) = await seed(client, 1)
    r = await client.post("/plan/item/update",
                          json={"item_id": a, "note": "corrected: the design changed"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text
    q = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    row = next(i for i in q.json()["items"] if i["item_id"] == a)
    assert row["note"] == "corrected: the design changed", "the note was not stored"


async def test_a_plain_bearer_cannot_update_an_item_either(client):
    """`update` moved gate at the same time `reorder` did, and only `reorder` had
    a test that a bearer alone is refused."""
    (a,) = await seed(client, 1)
    r = await client.post("/plan/item/update", json={"item_id": a, "note": "no"},
                          headers=LAPTOP)
    assert r.status_code == 403, r.text
    assert "X-Agent-Elevated" in r.text


async def test_caller_supplied_headers_cannot_redirect_the_actor(client):
    """`delegated()` forwards `key`, `requested` and `legacy_key` into `identify()`
    after validating the credential. None of them may turn the author into a person
    or into another machine — the whole provenance argument rests on the recorded
    actor being the one whose secret was checked."""
    a, b = await seed(client)
    r = await order_is(client, [b, a], {**LAPTOP_ELEVATED,
                                        "X-Agent-Name": "rich",
                                        "Remote-User": "rich"})
    assert r.status_code == 200, r.text
    by = r.json()["by"]
    assert by.split("/")[0] == "laptop", by
    assert not by.startswith("human/"), "a header turned an agent into a person"


async def test_a_delegated_partial_reorder_leaves_carried_rows_alone(client):
    """#183's rule — an item the caller did not list is carried along, not decided
    on, and keeps its prior `rank_source`. Pinned for the human path only."""
    a, b = await seed(client)
    (c,) = await seed(client, 1)
    before = (await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)).json()
    src = {i["item_id"]: i["rank_source"] for i in before["items"]}
    assert src[c] == "appended"
    r = await order_is(client, [b, a], LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text
    after = {i["item_id"]: i["rank_source"]
             for i in (await client.get("/plan", params={"repo": REPO},
                                        headers=LAPTOP)).json()["items"]}
    assert after[c] == "appended", "an unlisted row was marked as chosen"
    assert after[a] == after[b] == "derived"


# ------------------------------------------- how the secret map is read (#478)


def test_the_file_is_read_and_beats_the_inline_value(monkeypatch, tmp_path):
    """The production arrangement — op-resolver renders a file — and conftest pins
    the file to '' everywhere, so nothing else exercises this branch at all."""
    from app.config import Settings
    f = tmp_path / "ELEVATED_TOKENS"
    f.write_text("boxa:from-the-file\nboxb:also-from-the-file\n")
    st = Settings(elevated_tokens="ignored:inline", elevated_tokens_file=str(f))
    assert st.elevated_map == {"boxa": "from-the-file", "boxb": "also-from-the-file"}


def test_an_unreadable_file_is_a_closed_door_and_not_a_500(tmp_path):
    """This is read from inside an auth dependency. An OSError escaping it turns
    the documented closed refusal into an internal error — the one failure mode
    `_edge_asserted`'s "closed when no secret is configured" rule exists to avoid,
    arriving through the filesystem instead of through configuration."""
    from app.config import Settings
    st = Settings(elevated_tokens="", elevated_tokens_file=str(tmp_path / "gone"))
    assert st.elevated_map == {}
