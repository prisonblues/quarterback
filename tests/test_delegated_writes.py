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

**That third list is shorter since #591**, and the change is recorded here rather
than left for a reader to infer from a deleted test. `POST /dials` and
`POST /dials/clear` moved onto this credential on Rich's ask. The argument that
had kept them out was blast radius, and a dial does not have the shape that
argument feared: the row is cleared rather than deleted, it can carry an expiry,
and `set_via` names who turned it — so the "undo" #479 calls the highest-value
tightening for `/plan/reorder` is something `/dials` already had. `POST
/plan/scope` and `exempt`'s `grant: true` did NOT move, and still have their
tests below.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db import async_session
from app.models.dial import DialSetting

from .conftest import DESKTOP, LAPTOP, LAPTOP_ELEVATED, PINNED_SETTINGS, SERVER

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


@pytest.fixture
async def _no_dials_survive():
    """Clean in BOTH directions, for `test_human_key.py`'s reason and not tidiness.

    The schema is rebuilt once per session, so a dial set here is still in force in
    the next test and in the next FILE — and a fleet dial is returned by every
    repo-scoped read, so the failure surfaces in somebody else's assertion with
    nothing pointing back to this module. That is exactly how it bit when
    `test_human_key.py` was added, and this is the same guard.

    Requested by name rather than autouse: only the dial tests below need it, and
    a module-wide truncation would delete rows the plan tests here never made.
    """
    async with async_session() as s:
        await s.execute(delete(DialSetting))
        await s.commit()
    yield
    async with async_session() as s:
        await s.execute(delete(DialSetting))
        await s.commit()


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


async def test_a_delegated_agent_may_turn_a_dial(client, _no_dials_survive):
    """Reversed in #591, and the docstring it replaced is quoted so the change is
    legible rather than silent. It used to read: *"The whole point of a second
    credential rather than lending out the first: what it does NOT cover is where
    the blast radius stops. A dial is a judgement about what a review is worth and
    stays a person's."*

    What changed is not the appetite for blast radius but the observation that a
    dial is reversible where a plan order is not — see the module docstring.
    """
    r = await client.post("/dials", json={"dial": "review_panel.max_rounds",
                                          "value": 9, "reason": "asked to",
                                          "repo": REPO},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text


async def test_a_dial_an_agent_turned_is_not_recorded_as_a_persons(client,
                                                                   _no_dials_survive):
    """The property that made the reversal safe to make, and the reason this went
    on `delegated()` rather than handing an agent a person's key.

    `set_by` is the AGENT and `set_via` is `agent`. Had the capability arrived the
    way #479 records as rejected — an agent borrowing Rich's credential — both
    fields would have said `human/rich` and no later reader could have told a dial
    Rich turned from one an agent turned for him. That is `rank_source: derived`'s
    argument applied to a second table.
    """
    r = await client.post("/dials", json={"dial": "review_panel.max_rounds",
                                          "value": 4, "reason": "asked to",
                                          "repo": REPO},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text
    assert r.json()["by"].split("/")[0] == "laptop", r.json()["by"]
    assert not r.json()["by"].startswith("human/"), "a delegated agent is not a person"

    got = await client.get("/dials", params={"repo": REPO}, headers=LAPTOP)
    row = next(d for d in got.json()["dials"] if d["dial"] == "review_panel.max_rounds")
    assert row["set_via"] == "agent", row
    assert not row["set_by"].startswith("human/"), row


async def test_a_delegated_agent_may_also_clear_one(client, _no_dials_survive):
    """Both halves or neither. A dial an agent can set but not clear is a trap:
    the reversal is what makes the write safe to have granted, and #479's standard
    for this credential is reversibility rather than prevention."""
    await client.post("/dials", json={"dial": "review_panel.max_rounds",
                                      "value": 9, "reason": "asked to",
                                      "repo": REPO},
                      headers=LAPTOP_ELEVATED)
    r = await client.post("/dials/clear", json={"dial": "review_panel.max_rounds",
                                                "repo": REPO},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text
    assert [d["dial"] for d in r.json()["cleared"]] == ["review_panel.max_rounds"]

    got = await client.get("/dials", params={"repo": REPO}, headers=LAPTOP)
    assert "review_panel.max_rounds" not in [d["dial"] for d in got.json()["dials"]]


async def test_an_agent_may_not_clear_a_dial_a_person_set(client, _no_dials_survive):
    """THE BYPASS THE FIRST PASS LEFT OPEN, and the reason there is a rule here at
    all rather than only in `qb-start`.

    Guarding the WRITE guards the wrong half. `qb-start` refuses a
    `spawn.max_sessions` row authored by an agent, which stops an agent writing
    itself a bigger number — and does nothing about it deleting the smaller one a
    person wrote. Person sets 2, policy file says 8, agent clears the dial,
    `ceilings_from_board` reports no board ceiling, 8 applies. The ceiling has been
    raised to 8 without one agent-authored value ever being accepted.

    Absence is what the reader cannot interpret: "nobody set one" and "an agent
    removed the one somebody set" arrive there as the same empty answer. So it has
    to be refused where the difference is still visible.
    """
    r = await client.post("/dials", json={"dial": "spawn.max_sessions", "value": 2,
                                          "reason": "a person's ceiling"},
                          headers=HUMAN)
    assert r.status_code == 200, r.text

    r = await client.post("/dials/clear", json={"dial": "spawn.max_sessions"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 403, r.text
    assert "set by a person" in r.text

    got = await client.get("/dials", headers=LAPTOP)
    live = {d["dial"]: d for d in got.json()["dials"]}
    assert live["spawn.max_sessions"]["value"] == 2, "the person's ceiling survived"


async def test_an_agent_may_not_overwrite_a_dial_a_person_set(client, _no_dials_survive):
    """Replacing destroys the person's row exactly as clearing does — `set_dial`
    clears every live prior row before inserting — so the same rule has to cover it.
    Guarding only `clear` would leave the identical bypass one verb away."""
    await client.post("/dials", json={"dial": "review_panel.fix_severity_floor",
                                      "value": "P2", "reason": "a person's floor"},
                      headers=HUMAN)
    r = await client.post("/dials", json={"dial": "review_panel.fix_severity_floor",
                                          "value": "P4", "reason": "an agent's idea"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 403, r.text

    got = await client.get("/dials", headers=LAPTOP)
    live = {d["dial"]: d for d in got.json()["dials"]}
    assert live["review_panel.fix_severity_floor"]["value"] == "P2"


async def test_an_agent_may_replace_and_clear_its_own_dial(client, _no_dials_survive):
    """The rule is "not a person's", not "not anybody's". An agent that could set a
    dial and never correct it would be the trap the clear path was opened to avoid,
    one step along."""
    r = await client.post("/dials", json={"dial": "review_panel.max_rounds",
                                          "value": 3, "reason": "asked to"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text
    r = await client.post("/dials", json={"dial": "review_panel.max_rounds",
                                          "value": 4, "reason": "asked again"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text
    r = await client.post("/dials/clear", json={"dial": "review_panel.max_rounds"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 200, r.text


async def test_a_person_may_still_overwrite_an_agents_dial(client, _no_dials_survive):
    """The rule is one-directional and must be. A person who could not correct what
    an agent set would be locked out by a control meant to protect them — #479's own
    warning that "a gate nobody can satisfy is an outage"."""
    await client.post("/dials", json={"dial": "review_panel.max_rounds", "value": 9,
                                      "reason": "an agent's"},
                      headers=LAPTOP_ELEVATED)
    r = await client.post("/dials", json={"dial": "review_panel.max_rounds", "value": 2,
                                          "reason": "a person overriding"},
                          headers=HUMAN)
    assert r.status_code == 200, r.text
    assert r.json()["dial"]["set_via"] == "edge"


async def test_a_bare_machine_token_still_cannot_turn_a_dial(client, _no_dials_survive):
    """The half of the security argument that #591 did NOT reverse, and the one
    that actually protects a review from itself.

    `app.api.dials`' docstring names the threat: anything running while a branch
    under review is checked out — a test suite, a build step, a git hook — runs as
    a user whose machine token this board accepts, and could otherwise turn the
    `claude` seat off on the review of its own change. It holds the bearer by
    definition. It does not hold the delegated secret by definition, and that is
    now the whole of the distance between the two.
    """
    r = await client.post("/dials", json={"dial": "review_panel.max_rounds",
                                          "value": 9, "reason": "trying it on",
                                          "repo": REPO},
                          headers=LAPTOP)
    assert r.status_code == 403, r.text


async def test_one_machine_cannot_spend_anothers_secret_on_a_dial(client,
                                                                  _no_dials_survive):
    """The per-machine keying reaches the new endpoint too — it is a property of
    `delegated()`, not of the plan endpoints, and a test that only ever proved it
    at `/plan/reorder` would not have noticed if it had not."""
    r = await client.post("/dials", json={"dial": "review_panel.max_rounds",
                                          "value": 9, "reason": "wrong machine",
                                          "repo": REPO},
                          headers={**DESKTOP, "X-Agent-Elevated": "not-a-secret-laptop"})
    assert r.status_code == 403, r.text


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
    # Tied to the seeded rows, not an aggregate floor: `>= 2` in a shared scope
    # passes for an implementation that marked two OTHER items derived.
    src = {i["item_id"]: i["rank_source"] for i in body["items"]}
    assert src[a] == "derived" and src[b] == "derived"
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
    # Unconditional: `trusted` is defined as "no APPENDED rows", so the pin is
    # that derived rows do not enter that count. Wrapping it in `if appended == 0`
    # let the assertion evaporate in exactly the runs where the scope is busy —
    # which is every full-suite run.
    assert trust["trusted"] is (appended == 0), "derived rows changed `trusted`"
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
    q = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    row = next(i for i in q.json()["items"] if i["item_id"] == item)
    assert row["review"]["exempt"] is True, "the person's marker was not stored"


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
    q = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    row = next(i for i in q.json()["items"] if i["item_id"] == a)
    assert row["state"] == "open", "the refusal did not prevent the drop"


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
                                        "X-Agent-Key": "rich",
                                        "X-Agent-Instance": "rich",
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


def test_the_file_is_read_and_beats_the_inline_value(tmp_path):
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
    # An inline value IS set, which is the whole point: the old guard fell back to
    # it, so a test with an empty inline value passed either way and pinned nothing.
    st = Settings(elevated_tokens="boxa:inline-secret",
                  elevated_tokens_file=str(tmp_path / "gone"))
    assert st.elevated_map == {}, "an unreadable file fell back to the inline map"


async def test_a_delegated_agent_cannot_move_an_item_between_plans(client):
    """The gap a panel round escalated: the docstring said title and note, and
    `plan` was applied for a delegated caller with no check. Detaching an item from
    a plan somebody is holding is a decision, not a correction."""
    (a,) = await seed(client, 1)
    r = await client.post("/plan/item/update", json={"item_id": a, "plan": ""},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 403, r.text
    assert "plan" in r.json()["detail"]["refused"]


async def test_a_delegated_agent_cannot_revoke_a_persons_exemption(client):
    """The mirror of the round-1 P1, and the same field. `note` is a whole-field
    replacement, so an agent writing an innocuous note over one carrying the marker
    REVOKES the exemption and the PR silently rejoins the review queue.

    Round 1 closed "an agent may not set it"; this closes "an agent may not clear
    it". Measured before the guard: exempt True -> agent writes a note -> False.
    """
    r = await client.post("/plan/item",
                          json={"title": "an exempted pr", "repo": REPO,
                                "ref_kind": "pr", "ref_value": "7007"},
                          headers=LAPTOP)
    item = r.json()["item_id"]
    MINE.add(item)
    r = await client.post("/plan/item/update",
                          json={"item_id": item, "note": "review: exempt — release chore"},
                          headers=HUMAN)
    assert r.status_code == 200, r.text

    r = await client.post("/plan/item/update",
                          json={"item_id": item, "note": "picked this up"},
                          headers=LAPTOP_ELEVATED)
    assert r.status_code == 403, r.text
    assert "/plan/item/exempt" in r.text or "exempt" in r.text.lower()

    q = await client.get("/plan", params={"repo": REPO}, headers=LAPTOP)
    row = next(i for i in q.json()["items"] if i["item_id"] == item)
    assert row["review"]["exempt"] is True, "the agent revoked a person's exemption"


async def test_a_person_may_still_replace_the_note_on_an_exempted_item(client):
    """The guard refuses the ACT for an agent, not the field for everyone."""
    r = await client.post("/plan/item",
                          json={"title": "another exempted pr", "repo": REPO,
                                "ref_kind": "pr", "ref_value": "7008"},
                          headers=LAPTOP)
    item = r.json()["item_id"]
    MINE.add(item)
    await client.post("/plan/item/update",
                      json={"item_id": item, "note": "review: exempt — chore"},
                      headers=HUMAN)
    r = await client.post("/plan/item/update",
                          json={"item_id": item, "note": "changed my mind"},
                          headers=HUMAN)
    assert r.status_code == 200, r.text


async def test_a_machine_with_no_credential_is_told_that_not_that_it_mismatched(client):
    """Two different situations and the caller can act on only one. "Does not
    match" when nothing is configured sends somebody to check a secret against a
    map it is not in."""
    a, b = await seed(client)
    r = await order_is(client, [b, a], {**DESKTOP, "X-Agent-Elevated": "anything"})
    assert r.status_code == 403, r.text
    assert "no delegated credential configured" in r.text
    assert "does not match" not in r.text


async def test_a_non_ascii_CONFIGURED_secret_refuses_rather_than_500s(client, monkeypatch):
    """`hmac.compare_digest` raises TypeError on non-ASCII `str`, so the comparison
    is done on bytes.

    The reachable half is the CONFIGURED side, not the header: HTTP header values
    are ASCII/latin-1 and httpx refuses to send anything else, so a caller cannot
    get a non-ASCII value as far as the app. An operator can — `ELEVATED_TOKENS` is
    just text — and unfixed that turned every delegated request from that machine
    into a 500 out of an auth dependency rather than a refusal.
    """
    from app.config import settings
    monkeypatch.setattr(type(settings), "elevated_map",
                        property(lambda self: {"laptop": "sécret-ü"}))
    a, b = await seed(client)
    r = await order_is(client, [b, a], LAPTOP_ELEVATED)
    assert r.status_code == 403, r.text
