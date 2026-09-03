"""#427: taking a claim writes the plan item, at the top.

`hermes/seat-quarterback-1` filed #426 and claimed it eleven seconds later. Both
halves done properly, and the plan showed nothing — because the claim/item join
only ever ran item-first (`GET /plan` builds its key set *from the items it has*),
so a claim with no item behind it was looked up by nobody.

The properties under test are the ones that make "picking work up puts it on the
board" safe to do automatically:

* **A fresh claim on an issue writes the item, at the top of that scope**, held by
  the claimer — because the claim it derives is byte-for-byte the one the item
  keys on (#172), so the two halves join with nothing to reconcile.
* **It costs the human's order nothing WHILE THE CLAIM IS LIVE.** Every picked-up
  row is claimed at the moment it is written, and `next` skips claimed items, so
  the first free pick is exactly what it was. The qualifier is the whole of it:
  once the claim lapses or is released the row is a free item at rank 1 and it
  DOES become `next`, ahead of the human's list. That is defensible — abandoned
  work is a good thing to pick up — but it may not arrive silently, so `next`
  carries a caveat saying what the row actually is.
* **The vocabulary stays honest.** `picked-up` is its own `rank_source` and counts
  as chosen, so a busy fleet does not make the plan read as untrustworthy.
* **It writes an item only for a unit of work.** A merge claim, a board object, a
  path key (#185) and the open namespace all write nothing — and that silence is
  a normal answer, not an error.
* **The claim is what is guaranteed, and a renew REPAIRS.** The item never fails
  the claim — and because it can fail, a renew runs the same idempotent write, so
  a claim whose item was lost to a transient fault is not invisible forever.
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import DESKTOP, LAPTOP, PINNED_SETTINGS, SERVER

#: A person, as the edge proves it — the identity header AND the secret only
#: the proxy knows. Reordering is human-only, and that is the point of it.
HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}


async def claim(client, headers=LAPTOP, **body) -> dict:
    r = await client.post("/claim", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def claim_issue(client, repo: str, number: int, headers=LAPTOP, **over) -> dict:
    return await claim(client, headers=headers,
                       ref={"kind": "issue", "repo": repo, "value": str(number)}, **over)


async def read(client, repo: str | None = None, headers=LAPTOP, **params) -> dict:
    r = await client.get("/plan", params={**({"repo": repo} if repo else {}), **params},
                         headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _expire(key: str) -> None:
    """Age a live claim out, instead of waiting for the wall clock to do it."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.api.plan import CLAIM_KIND
    from app.db import async_session
    from app.models.resource_lease import ResourceLease

    async with async_session() as s:
        await s.execute(
            update(ResourceLease)
            .where(ResourceLease.kind == CLAIM_KIND, ResourceLease.key == key,
                   ResourceLease.released_at.is_(None))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()


async def add(client, repo: str, title: str, headers=LAPTOP, **over) -> dict:
    r = await client.post("/plan/item", json={"repo": repo, "title": title, **over},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------- the item a claim implies

async def test_claiming_an_issue_puts_it_on_the_plan_held_by_the_claimer(client):
    """The whole of #426's complaint, as one call: claim the issue, read the plan,
    and the work is there with a holder on it — no second call, no hand-add."""
    repo = "acme/pickup"
    out = await claim_issue(client, repo, 426, note="Port REVIEW QUEUE into the TUI")

    item = out["plan_item"]
    assert item is not None, out
    assert (item["rank"], item["rank_source"]) == (1, "picked-up")
    assert item["title"] == "Port REVIEW QUEUE into the TUI"

    plan = await read(client, repo)
    row = next(i for i in plan["items"] if i["item_id"] == item["item_id"])
    assert row["ref"] == {"kind": "issue", "value": "426"}
    # The point of #172's derived key, demonstrated: the claim taken by `POST
    # /claim` IS the claim the item reads, with nothing joining them by hand.
    assert row["claim"]["holder"] == "laptop"
    assert row["claim"]["key"] == f"{repo}#426"


async def test_a_pr_claim_is_work_too(client):
    repo = "acme/prwork"
    out = await claim(client, ref={"kind": "pr", "repo": repo, "value": "207"},
                      note="land it")
    assert out["plan_item"]["rank_source"] == "picked-up"
    plan = await read(client, repo)
    assert plan["items"][0]["ref"] == {"kind": "pr", "value": "207"}


async def test_a_composed_key_writes_the_same_item_as_a_derived_one(client):
    """The #172 compatibility path reaches the plan as well. An agent that has not
    been updated still composes `kind='issue'`, and must not thereby be the one
    agent whose pickups are invisible."""
    repo = "acme/composed"
    out = await claim(client, kind="issue", key=f"{repo}#88", note="composed by hand")
    assert out["plan_item"]["title"] == "composed by hand"
    plan = await read(client, repo)
    assert plan["items"][0]["ref"] == {"kind": "issue", "value": "88"}


# ----------------------------------------- it costs the human's order nothing

async def test_a_held_pickup_does_not_displace_next(client):
    """The property the feature rests on, stated with its qualifier. Picked-up rows
    sit above the human's list and `next` walks straight past them BECAUSE THEY ARE
    CLAIMED — so while they are held, the first free pick is the one it would have
    been. What happens when the claim goes is a different answer, and it is
    `test_an_abandoned_pickup_becomes_next_but_says_so`: this test is not evidence
    for that one."""
    repo = "acme/nextsafe"
    human_top = await add(client, repo, "the thing Rich actually wants first",
                          ref_kind="issue", ref_value="63")
    assert (await read(client, repo))["next"]["item_id"] == human_top["item_id"]

    await claim_issue(client, repo, 426, note="in flight")
    await claim_issue(client, repo, 427, headers=SERVER, note="also in flight")

    plan = await read(client, repo)
    # Both pickups are above it...
    assert [i["ref"]["value"] for i in plan["items"]] == ["427", "426", "63"]
    # ...and it is still the answer.
    assert plan["next"]["item_id"] == human_top["item_id"]
    assert plan["next"]["rank"] == 3


async def test_an_abandoned_pickup_becomes_next_but_says_so(client):
    """The counterexample to the unqualified version of the safety claim, and the
    reason `next` grew a second caveat.

    A pickup is promoted to rank 1 on the strength of a live claim. When the claim
    goes — the agent's box died, the TTL lapsed, somebody released it — the row is
    open, unclaimed and unblocked at rank 1, so it becomes `next` ahead of a list a
    human ordered. Being `next` at all is proof the justification expired, because
    `next` skips anything claimed.

    Not demoted and not hidden: work somebody started and put down is a good thing
    to pick up. But it arrives as a qualified answer, because a silent promotion
    over a human's stated order is the plan asserting a priority nobody set."""
    repo = "acme/abandoned"
    human = await add(client, repo, "Rich's actual top priority",
                      ref_kind="issue", ref_value="63")
    r = await client.post("/plan/reorder",
                          json={"repo": repo, "order": [human["item_id"]]}, headers=HUMAN)
    assert r.status_code == 200, r.text

    out = await claim_issue(client, repo, 999, note="agent picks this up, then dies")
    live = await read(client, repo)
    assert live["next"]["ref"]["value"] == "63", "while held, the human's order stands"
    assert live["next"]["caveat"] is None

    await _expire(f"{repo}#999")

    gone = await read(client, repo)
    assert gone["next"]["item_id"] == out["plan_item"]["item_id"]
    assert gone["next"]["ref"]["value"] == "999"
    # The whole point: it is the answer AND it is qualified.
    caveat = gone["next"]["caveat"]
    assert caveat and "claim is gone" in caveat
    assert "not a position anybody ranked" in caveat


async def test_the_two_caveats_are_said_together_when_both_apply(client):
    """An abandoned row at the top of an order nobody chose is two separate
    problems, and a caveat that mentions one is read as absolving the other."""
    repo = "acme/bothcaveats"
    await add(client, repo, "appended, nobody chose this", ref_kind="issue", ref_value="63")
    await claim_issue(client, repo, 999, note="picked up then dropped")
    await _expire(f"{repo}#999")

    plan = await read(client, repo)
    caveat = plan["next"]["caveat"]
    assert "claim is gone" in caveat, "the abandoned half"
    assert "nobody chose those positions" in caveat, "the untrusted-order half"


async def test_a_held_pickup_is_never_qualified(client):
    """The caveat must not fire on the ordinary case, or it is noise on every read."""
    repo = "acme/quiet"
    human = await add(client, repo, "ordered", ref_kind="issue", ref_value="63")
    await client.post("/plan/reorder", json={"repo": repo, "order": [human["item_id"]]},
                      headers=HUMAN)
    await claim_issue(client, repo, 999, note="in flight")
    assert (await read(client, repo))["next"]["caveat"] is None


async def test_the_newest_pickup_goes_on_top_of_the_older_one(client):
    """Top means top. Two agents picking work up leaves the most recent first,
    and neither displaces anything below on merit."""
    repo = "acme/stack"
    await claim_issue(client, repo, 1, note="first")
    await claim_issue(client, repo, 2, headers=SERVER, note="second")
    await claim_issue(client, repo, 3, headers=DESKTOP, note="third")

    plan = await read(client, repo)
    assert [i["ref"]["value"] for i in plan["items"]] == ["3", "2", "1"]
    assert [i["rank"] for i in plan["items"]] == [1, 2, 3]


async def test_a_busy_fleet_does_not_make_the_plan_read_as_untrusted(client):
    """`picked-up` counts as CHOSEN. Had it been spelled `appended`, every claim
    taken would have added a position "nobody chose" and `next` would have carried
    a caveat about the human's ordering for the sole reason that agents were
    working — two different signals collapsed into one."""
    repo = "acme/trust"
    human = await add(client, repo, "set by a person", ref_kind="issue", ref_value="63")
    # A human actually orders it, so the plan starts out trusted and the pickup is
    # the only thing that could take that away.
    r = await client.post("/plan/reorder",
                          json={"repo": repo, "order": [human["item_id"]]}, headers=HUMAN)
    assert r.status_code == 200, r.text
    assert (await read(client, repo))["order_trust"]["trusted"] is True

    await claim_issue(client, repo, 426, note="in flight")

    plan = await read(client, repo)
    assert plan["order_trust"]["trusted"] is True
    assert plan["order_trust"]["unchosen"] == 0
    assert plan["order_trust"]["by_source"] == {"ordered": 1, "picked-up": 1}
    assert plan["next"]["caveat"] is None


# ----------------------------------------- only a unit of work

@pytest.mark.parametrize("body,why", [
    ({"ref": {"kind": "branch", "repo": "acme/x", "value": "main"}},
     "a base branch is not a unit of work (#318) — the issue behind the land has its own claim"),
    ({"kind": "work", "key": "acme/x:serving-row:32022R2554"},
     "the open namespace: a row in somebody's database is not the plan's subject"),
    ({"kind": "work", "key": "hermes:wt-427:app/api/plan.py"},
     "a path key (#185) — contended by agents sharing a directory, not by the fleet"),
    ({"kind": "merge", "key": "acme/x:feat/thing"},
     "a merge claim, however it is spelled"),
])
async def test_a_claim_that_names_no_work_writes_no_item(client, body, why):
    """And says nothing about it. Silence is the normal answer on these paths, so
    reporting one would be noise on the majority of claims the board takes."""
    out = await claim(client, note="not work", **body)
    assert out["claimed"] is True, why
    assert out["plan_item"] is None, why
    assert "plan_item_error" not in out, why


async def test_claiming_a_plan_item_does_not_write_a_second_item(client):
    """`plan_claim` goes through `acquire` too, and an item claiming itself into
    existence is a loop with nothing at the end of it. The hook is on the endpoint
    for exactly this reason."""
    repo = "acme/selfref"
    item = await add(client, repo, "already here", ref_kind="issue", ref_value="9")
    r = await client.post("/plan/item/claim", json={"item_id": item["item_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text

    plan = await read(client, repo)
    assert len(plan["items"]) == 1
    assert plan["items"][0]["rank_source"] != "picked-up"


# ----------------------------------------- the claim is what is guaranteed

async def test_a_renew_returns_the_same_item_rather_than_a_second_one(client):
    """A renew runs the same write, and the write is idempotent: one open item per
    issue is the database's rule, so the existing row comes back untouched rather
    than being rewritten with the renewing note."""
    repo = "acme/renew"
    first = await claim_issue(client, repo, 5, session="s1", note="mine")
    again = await claim_issue(client, repo, 5, session="s1", note="still mine")

    assert again["renewed"] is True
    assert again["plan_item"]["item_id"] == first["plan_item"]["item_id"]
    assert again["plan_item"]["title"] == "mine", "a renew must not rewrite the item"
    plan = await read(client, repo)
    assert len(plan["items"]) == 1


async def test_a_renew_repairs_an_item_the_first_claim_failed_to_write(client, monkeypatch):
    """The hole a `if not renewed` gate left, and the reason there is no such gate.

    The plan write is best-effort, so it can fail — a transient database fault, a
    deploy caught mid-migration. The claim survives that by design. But if only a
    FRESH take could write the item, the claim was then invisible on the plan for
    as long as it kept being renewed, and a renewing agent renews for hours. A
    feature built to abolish claim-only invisibility would have manufactured a
    durable instance of it on its first bad day."""
    import app.api.plan as plan_api

    real = plan_api.item_for_claim

    async def boom(*a, **kw):
        raise RuntimeError("transient: the plan table is unavailable")

    repo = "acme/repair"
    monkeypatch.setattr(plan_api, "item_for_claim", boom)
    first = await claim_issue(client, repo, 7, session="s1", note="picking up")
    assert first["claimed"] is True
    assert first["plan_item"] is None
    assert (await read(client, repo))["items"] == [], "nothing on the plan yet"

    # The fault clears, and the agent does what a holding agent does: renews.
    monkeypatch.setattr(plan_api, "item_for_claim", real)
    again = await claim_issue(client, repo, 7, session="s1", note="picking up")
    assert again["renewed"] is True
    assert again["plan_item"] is not None, "the renew must repair, not skip"

    plan = await read(client, repo)
    assert len(plan["items"]) == 1
    assert plan["items"][0]["ref"] == {"kind": "issue", "value": "7"}
    assert plan["items"][0]["claim"]["holder"] == "laptop"


async def test_claiming_an_issue_already_on_the_plan_returns_that_item(client):
    """One open item per issue is the database's rule and its answer here is
    success. The claim is what prevents duplicated work; the item is a consequence
    of it, and a claim refused because the row already existed would invert that."""
    repo = "acme/planned"
    existing = await add(client, repo, "a human wrote this one",
                         ref_kind="issue", ref_value="60", note="why it sits here")

    out = await claim_issue(client, repo, 60, note="picking it up")
    assert out["claimed"] is True
    assert out["plan_item"]["item_id"] == existing["item_id"]
    # Untouched: the human's title, note and position all survive the pickup.
    assert out["plan_item"]["title"] == "a human wrote this one"
    assert out["plan_item"]["rank_source"] == "appended"

    plan = await read(client, repo)
    assert len(plan["items"]) == 1
    assert plan["items"][0]["claim"]["holder"] == "laptop"


async def test_the_second_claimant_of_planned_work_is_still_refused_the_claim(client):
    """The item being there changes nothing about exclusivity — a 409 naming the
    holder is still a 409, and it must not become a 200 because the plan row
    already existed."""
    repo = "acme/contended"
    await claim_issue(client, repo, 77, note="mine")
    r = await client.post("/claim", json={
        "ref": {"kind": "issue", "repo": repo, "value": "77"}, "note": "mine too"},
        headers=SERVER)
    assert r.status_code == 409, r.text
    assert "laptop" in r.text


# ----------------------------------------- naming the work

async def test_a_plan_write_that_fails_does_not_take_the_claim_with_it(client, monkeypatch):
    """The safety property the whole design rests on, forced rather than argued.

    A claim that lands with no item costs a row on a dashboard. A claim REFUSED
    because a plan write failed costs the duplicated work the claim exists to
    prevent, and they are not close — so the failure is reported beside a claim
    that stands, never instead of one."""
    import app.api.plan as plan_api

    async def boom(*a, **kw):
        raise RuntimeError("the plan table is on fire")

    monkeypatch.setattr(plan_api, "item_for_claim", boom)

    repo = "acme/onfire"
    out = await claim_issue(client, repo, 99, note="picking up regardless")
    assert out["claimed"] is True
    assert out["plan_item"] is None
    assert "on fire" in out["plan_item_error"]

    # And the claim is real: a second agent is still refused by it.
    r = await client.post("/claim", json={
        "ref": {"kind": "issue", "repo": repo, "value": "99"}}, headers=SERVER)
    assert r.status_code == 409, r.text


async def test_a_client_that_read_the_forge_may_pass_the_real_title(client):
    """The "clients enrich" half. The server cannot read GitHub (#327) and does
    not try; a caller that just ran `gh issue view` passes what it found."""
    repo = "acme/titled"
    out = await claim_issue(client, repo, 426, note="port the panel",
                            title="The dash pane defaults to the renderer nobody chose")
    assert out["plan_item"]["title"] == "The dash pane defaults to the renderer nobody chose"


async def test_absent_a_title_the_claim_note_names_it(client):
    """`note` is already specified as "one line on what you are doing with it",
    which is the same sentence a plan title wants."""
    out = await claim_issue(client, "acme/noted", 12, note="flip the default")
    assert out["plan_item"]["title"] == "flip the default"


async def test_absent_both_it_is_the_ref_and_visibly_a_placeholder(client):
    """A made-up handle would be worse than a bare one: a reader who sees `#12`
    knows to open the issue, where an invented title reads as somebody's summary."""
    out = await claim_issue(client, "acme/bare", 12)
    assert out["plan_item"]["title"] == "#12"


async def test_a_blank_note_is_not_a_title(client):
    """`min_length` passes `" "`, and a row whose title renders as an empty cell
    is the thing `_norm_text` exists to stop."""
    out = await claim_issue(client, "acme/blank", 13, note="   ")
    assert out["plan_item"]["title"] == "#13"


async def test_an_overlong_title_is_cut_rather_than_refused(client):
    """The claim must survive a caller that pasted an essay into `note`."""
    out = await claim_issue(client, "acme/long", 14, note="x" * 400)
    assert out["claimed"] is True
    assert len(out["plan_item"]["title"]) == 200


# ----------------------------------------- what the row says about itself

async def test_the_item_records_who_picked_it_up_and_claims_no_priority(client):
    """`placed_for` stays null. It transcribes whose stated PRIORITY a position
    records, and nobody stated one — the row is at the top because work started on
    it, which the note says in those words."""
    repo = "acme/provenance"
    out = await claim_issue(client, repo, 426, headers=SERVER, note="in flight")
    plan = await read(client, repo)
    row = next(i for i in plan["items"] if i["item_id"] == out["plan_item"]["item_id"])

    assert row["added_by"] == "server"
    assert row["placed_for"] is None
    assert "Picked up by server" in row["note"]
    # Which of a repo's plans this belongs under is a judgement about the work,
    # and nothing at claim time knows it.
    assert row["plan"] is None


async def test_a_lapsed_claim_leaves_the_item_behind_unclaimed(client):
    """The item outlives the claim on purpose. A session that dies frees the work
    without anybody intervening, and what is left is a plan row saying somebody
    thought this was worth starting — which is a better plan than one that forgets."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.api.plan import CLAIM_KIND
    from app.db import async_session
    from app.models.resource_lease import ResourceLease

    repo = "acme/lapsed"
    out = await claim_issue(client, repo, 31, note="started this")
    async with async_session() as s:
        await s.execute(
            update(ResourceLease)
            .where(ResourceLease.kind == CLAIM_KIND, ResourceLease.key == f"{repo}#31",
                   ResourceLease.released_at.is_(None))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()

    plan = await read(client, repo)
    row = next(i for i in plan["items"] if i["item_id"] == out["plan_item"]["item_id"])
    assert row["claim"] is None
    assert row["state"] == "open"
    # And now that nobody holds it, it is the thing to do next.
    assert plan["next"]["item_id"] == row["item_id"]


async def test_releasing_a_claim_does_not_finish_the_item(client):
    """Stopping is not finishing. `plan_done` records that the issue closed and
    stays explicit — a release that closed the item would quietly retire work the
    moment somebody put it down."""
    repo = "acme/released"
    out = await claim_issue(client, repo, 41, note="picking up")
    r = await client.post("/claim/release", json={"claim_id": out["claim_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text

    plan = await read(client, repo)
    row = next(i for i in plan["items"] if i["item_id"] == out["plan_item"]["item_id"])
    assert row["state"] == "open"
    assert row["done"] is None


async def test_scopes_do_not_leak_into_each_other(client):
    """Ranks are allocated per scope, so a pickup in one repo must not renumber
    another's — `_top_rank` reads the scope EXACTLY, like its two siblings."""
    a, b = "acme/left", "acme/right"
    await add(client, a, "left one", ref_kind="issue", ref_value="1")
    await add(client, b, "right one", ref_kind="issue", ref_value="1")
    await claim_issue(client, a, 99, note="only in left")

    left, right = await read(client, a), await read(client, b)
    assert [i["rank"] for i in left["items"]] == [1, 2]
    assert [i["ref"]["value"] for i in left["items"]] == ["99", "1"]
    assert [(i["rank"], i["ref"]["value"]) for i in right["items"]] == [(1, "1")]


async def test_an_unparseable_repo_in_a_stored_key_costs_no_claim(client):
    """`acme/foo.git#12` matches the key shape and is not a repo this board can
    name. The truthful answer is "no resource I can put on a plan", and the claim
    must land regardless — it is the half that prevents duplicated work."""
    out = await claim(client, kind="work", key="acme/foo.git#12", note="legacy shape")
    assert out["claimed"] is True
    assert out["plan_item"] is None


async def test_a_fleet_wide_key_is_not_invented(client):
    """Every work key this writes an item for carries a repo, so no pickup can
    land in the NULL scope by accident — the fleet band is for items a person put
    there deliberately."""
    before = len((await read(client))["items"])
    await claim(client, kind="work", key=f"plan:{uuid.uuid4()}", note="board object")
    assert len((await read(client))["items"]) == before


# ----------------------------------------- exclusivity without a pickup (#722)

async def claim_pr(client, repo: str, number: int, headers=LAPTOP, **over) -> dict:
    return await claim(client, headers=headers,
                       ref={"kind": "pr", "repo": repo, "value": str(number)}, **over)


async def test_a_claim_that_is_not_a_pickup_writes_no_item(client):
    """#722. #715 made a panel review round claim the PR it is reading, so the
    fleet can see which agent is spending money on which PR. That claim is a true
    exclusivity record and a false pickup: it wrote the PR onto the plan at rank 1,
    with `rank_source: picked-up`, on behalf of an agent that was reviewing
    somebody else's work rather than starting any.

    The claim is unchanged — it is still exclusive, it still refuses a second
    holder, it still names the round in its note. Only the assertion that work has
    been picked up is withdrawn."""
    repo = "acme/reviewed"
    out = await claim_pr(client, repo, 715, plan_item=False,
                         note="panel review round 1")
    assert out["claimed"] is True
    assert out["plan_item"] is None
    assert "plan_item_error" not in out, "not a failure — nothing was attempted"
    assert (await read(client, repo))["items"] == []


async def test_a_round_that_claims_and_releases_leaves_the_plan_as_it_found_it(client):
    """The consequence, end to end, and the reason the flag is worth a field.

    Step for step it is the issue: a round starts and claims the PR; the round
    ends and releases it. Before this the plan then held an item for that PR —
    open, unclaimed, unblocked, rank 1 — so `next` handed the following agent a
    review that had already happened, above the order a human had actually set.
    Read off this board while #722 was open: `next` for this repo was the item for
    PR #715 — the PR that added the round's claim — open at rank 8, its own claim
    released, ahead of every ordered item below it."""
    repo = "acme/roundtrip"
    human = await add(client, repo, "the thing Rich actually wants first",
                      ref_kind="issue", ref_value="63")
    r = await client.post("/plan/reorder",
                          json={"repo": repo, "order": [human["item_id"]]}, headers=HUMAN)
    assert r.status_code == 200, r.text

    held = await claim_pr(client, repo, 715, plan_item=False,
                          note="panel review round 1")
    r = await client.post("/claim/release", json={"claim_id": held["claim_id"]},
                          headers=LAPTOP)
    assert r.status_code == 200, r.text

    plan = await read(client, repo)
    assert [i["item_id"] for i in plan["items"]] == [human["item_id"]]
    assert plan["next"]["item_id"] == human["item_id"]
    assert plan["next"]["caveat"] is None


async def test_the_default_is_still_a_pickup(client):
    """#427's rule is right for a real pickup and nothing about it changes. The
    flag has to be asked for, so every caller that has not heard of it — every
    agent, hook and skill on the fleet — goes on putting its work on the board."""
    repo = "acme/stillapickup"
    out = await claim_issue(client, repo, 426, note="picking this up")
    assert out["plan_item"]["rank"] == 1
    assert out["plan_item"]["rank_source"] == "picked-up"
    assert len((await read(client, repo))["items"]) == 1


async def test_a_pr_claim_is_a_pickup_unless_the_caller_says_otherwise(client):
    """The alternative fix, refused: teaching the board that a PR claim is never a
    pickup. Sometimes it is exactly that — an agent taking over somebody's stalled
    PR to finish it — and nothing in the key can tell the two apart. The caller
    can, so the caller says."""
    repo = "acme/prpickup"
    out = await claim_pr(client, repo, 207, note="finishing this one off")
    assert out["plan_item"]["rank_source"] == "picked-up"
    assert (await read(client, repo))["items"][0]["ref"] == {"kind": "pr", "value": "207"}


async def test_it_does_not_retire_an_item_that_is_already_there(client):
    """"Do not write one" and "retire the one you find" are different powers, and a
    flag on a claim must not carry the second. The row a round would have retired
    may be a human's, or a real pickup's by an agent still working; the claim path
    cannot tell, and a review that quietly cleared the plan row for the PR it was
    reading would be a worse defect than the one this fixes."""
    repo = "acme/alreadyplanned"
    existing = await add(client, repo, "somebody is finishing this PR",
                         ref_kind="pr", ref_value="715")

    out = await claim_pr(client, repo, 715, plan_item=False,
                         note="panel review round 1")
    assert out["claimed"] is True
    assert out["plan_item"] is None, "it reports no item, and it wrote none"

    plan = await read(client, repo)
    assert [i["item_id"] for i in plan["items"]] == [existing["item_id"]]
    assert plan["items"][0]["title"] == "somebody is finishing this PR"
    assert plan["items"][0]["state"] == "open"


async def test_a_renew_without_the_item_does_not_repair_one_into_existence(client):
    """A renew repairs a plan write that failed (#427), and that repair must not
    fire for a caller that never wanted the item. A round renews for as long as it
    is reviewing, so a repairing renew would put the PR on the plan on the second
    heartbeat and the flag would buy nothing but a delay."""
    repo = "acme/renewnoitem"
    first = await claim_pr(client, repo, 715, session="s1", plan_item=False,
                           note="panel review round 1")
    again = await claim_pr(client, repo, 715, session="s1", plan_item=False,
                           note="panel review round 1")
    assert first["claimed"] is True and again["renewed"] is True
    assert again["plan_item"] is None
    assert (await read(client, repo))["items"] == []


async def test_a_title_is_ignored_rather_than_refused_when_no_item_will_hold_it(client):
    """The item is the title's only consumer, so with no item there is nothing for
    it to name. Ignored and not refused: a claim turned down over a redundant field
    costs the duplicated work the claim exists to prevent, which is the trade this
    endpoint declines everywhere else."""
    out = await claim_pr(client, "acme/titleignored", 715, plan_item=False,
                         title="fix: the thing the round is reading")
    assert out["claimed"] is True
    assert out["plan_item"] is None


async def test_the_claim_is_as_exclusive_as_any_other(client):
    """What the flag withdraws is the assertion of a pickup and nothing else. A
    second agent is still refused, and told who to talk to — which is the whole
    reason a round takes a claim at all."""
    repo = "acme/stillexclusive"
    await claim_pr(client, repo, 715, plan_item=False, note="panel review round 1")
    r = await client.post("/claim", json={
        "ref": {"kind": "pr", "repo": repo, "value": "715"},
        "note": "my round too", "plan_item": False}, headers=SERVER)
    assert r.status_code == 409, r.text
    assert "laptop" in r.text
