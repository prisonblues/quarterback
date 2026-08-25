"""What a reconcile pass found, carried by the plan that needs it (#463).

The failure these are written against, with its own times on it. At 10:40Z a plan
read answered ``next: #449``. #449 had been closed as completed at 07:33Z. The
reconcile pass at 10:33Z had said so, naming it by rank, and had posted it to this
same board. Both facts were here; no reader saw them together, and the caveat that
did fire sent the reader off to ask a peer whether work the board already knew was
finished had been abandoned instead.

Two of the three items at the top of that list were closed. A third had been closed
for over two days.
"""

from __future__ import annotations

import pytest

from .conftest import DESKTOP, LAPTOP

pytestmark = pytest.mark.asyncio


#: Every item this file creates, so the fixture below can put it back.
_MADE: list[str] = []


@pytest.fixture(autouse=True)
async def _tidy_up(client):
    """Finish this file's items when the test ends.

    The suite shares ONE database for the whole run, and `qbdata.fetch_plan` — the
    dashboard's read, which `test_plans.py` exercises — asks for the fleet-wide
    plan one `PLAN_LIMIT` page at a time, 200 rows, no repo filter. Open items
    from every file that ran earlier are on that page, so a file leaving a dozen
    behind pushes a later file's row off the end of it, and that test fails
    looking for its own item.

    A `done` item is out of the default read, so tidying up costs one call and
    puts this file's footprint back to nothing. It does not make the underlying
    cliff go away — see the note in the issue — it just declines to be the twelve
    rows that walk somebody else off it.
    """
    yield
    while _MADE:
        await client.post("/plan/item/done", json={"item_id": _MADE.pop()},
                          headers=LAPTOP)


async def add(client, repo: str, title: str, headers=LAPTOP, **over) -> dict:
    r = await client.post("/plan/item", json={"repo": repo, "title": title, **over},
                          headers=headers)
    assert r.status_code == 200, r.text
    _MADE.append(r.json()["item_id"])
    return r.json()


async def issue(client, repo: str, number: int, **over) -> dict:
    return await add(client, repo, f"#{number}", ref_kind="issue",
                     ref_value=str(number), **over)


async def read(client, repo: str | None = None, headers=LAPTOP, **params) -> dict:
    r = await client.get("/plan", params={**({"repo": repo} if repo else {}), **params},
                         headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def report(client, repo: str, findings: list[dict], headers=LAPTOP) -> dict:
    r = await client.post("/plan/reconcile", json={"repo": repo, "findings": findings},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def done(number: int, said: str = "open item, but the issue is closed as completed") -> dict:
    return {"ref_kind": "issue", "ref_value": str(number),
            "condition": "done_candidate", "said": said}


def item_for(plan: dict, number: int) -> dict:
    return next(i for i in plan["items"] if (i["ref"] or {}).get("value") == str(number))


# ------------------------------------------------- the read carries what the pass saw


async def test_the_plan_carries_what_the_last_pass_found(client):
    """The join that did not exist. One call reports it, the next read has it."""
    repo = "acme/carried"
    await issue(client, repo, 449)
    await issue(client, repo, 450)

    await report(client, repo, [done(449, "issue#449 is closed as completed")])
    plan = await read(client, repo, exact="true")

    flagged = item_for(plan, 449)["reconcile"]
    assert flagged["condition"] == "done_candidate"
    assert flagged["said"] == "issue#449 is closed as completed"
    assert flagged["days"] == 0.0
    # Null rather than absent on the item nothing was said about: a client reading
    # `.get("reconcile")` must be able to tell "the pass cleared it" from "this
    # board is too old to have the field".
    assert item_for(plan, 450)["reconcile"] is None


async def test_next_says_the_work_is_already_finished(client):
    """The regression test for 10:40Z, and the whole point of the change.

    `next` still ANSWERS the item — it is not skipped, hidden or marked done. What
    it no longer does is answer it as though the board knew nothing.
    """
    repo = "acme/alreadydone"
    first = await issue(client, repo, 449)

    await report(client, repo, [done(449)])
    plan = await read(client, repo, exact="true")

    assert plan["next"]["item_id"] == first["item_id"], "the item was skipped, not caveated"
    assert plan["next"]["state"] == "open"
    caveat = plan["next"]["caveat"]
    assert "ALREADY FINISHED" in caveat
    assert "closed as completed" in caveat
    assert "plan_done" in caveat, "the caveat has to say what to do about it"


async def test_the_finished_caveat_comes_before_the_ones_about_ordering(client):
    """A reader who stops after one sentence gets the one that changes their action.

    All three can apply at once. "This is already done" changes what you do
    completely; "ranks below 21 are unchosen" hardly at all.
    """
    # A repo NO OTHER FILE uses. The suite shares one database for the whole run,
    # so a name another file has already put a picked-up item in makes `next`
    # somebody else's row — which is what this pair of tests found the hard way.
    repo = "acme/reconcile-ordering"
    await issue(client, repo, 449)          # appended, so the order is untrusted too

    await report(client, repo, [done(449)])
    caveat = (await read(client, repo, exact="true"))["next"]["caveat"]

    assert caveat.index("ALREADY FINISHED") < caveat.index("nobody chose")


async def test_an_abandoned_pass_is_a_judgement_and_says_so(client):
    """`dropped_candidate` must not read like `done_candidate`.

    This is the distinction `qb-reconcile` refuses to collapse, and the reason the
    condition had to reach the board as data rather than through a post's refs:
    one is a record already overtaken, the other is a decision somebody has to
    make. Through the post they arrive identical.
    """
    repo = "acme/reconcile-dropped"
    await issue(client, repo, 451)

    await report(client, repo, [{"ref_kind": "issue", "ref_value": "451",
                                 "condition": "dropped_candidate",
                                 "said": "closed as not planned"}])
    caveat = (await read(client, repo, exact="true"))["next"]["caveat"]

    assert "abandoned rather than completed" in caveat
    assert "decision nobody has made" in caveat
    assert "ALREADY FINISHED" not in caveat


async def test_a_condition_this_board_has_not_been_taught_still_reads(client):
    """Fails open, on purpose.

    The conditions belong to a client on another host, which updates when its
    harness does. A board that refused a pass for carrying one word it did not
    know would fail closed on exactly the day somebody added a condition — losing
    the other findings in the same report with it.
    """
    repo = "acme/futurecondition"
    await issue(client, repo, 452)

    await report(client, repo, [{"ref_kind": "issue", "ref_value": "452",
                                 "condition": "invented_next_year",
                                 "said": "something new"}])
    plan = await read(client, repo, exact="true")

    assert item_for(plan, 452)["reconcile"]["condition"] == "invented_next_year"
    assert "`invented_next_year`" in plan["next"]["caveat"]
    assert "something new" in plan["next"]["caveat"]


# ------------------------------------------------------------------ replace semantics


async def test_a_ref_the_pass_stops_naming_is_resolved(client):
    """The write replaces a scope's set, so silence is the resolution.

    A pass reports what it still finds. Requiring a separate "this one is fine
    now" call is a call that gets forgotten, and the rows outlive the disagreement
    they describe — which is the failure one level down from the one this fixes.
    """
    repo = "acme/resolves"
    await issue(client, repo, 453)

    await report(client, repo, [done(453)])
    assert item_for(await read(client, repo, exact="true"), 453)["reconcile"] is not None

    out = await report(client, repo, [])
    assert out["resolved"] == 1 and out["stored"] == 0
    assert item_for(await read(client, repo, exact="true"), 453)["reconcile"] is None


async def test_reporting_one_scope_leaves_another_scopes_findings_alone(client):
    """Per scope, because that is the unit being replaced.

    Reported together, an absent ref would be absent from both — silently
    resolving findings for a repo the pass never spoke about.
    """
    mine, theirs = "acme/reported", "acme/untouched"
    await issue(client, mine, 454)
    await issue(client, theirs, 455)
    await report(client, mine, [done(454)])
    await report(client, theirs, [done(455)])

    await report(client, mine, [])

    assert item_for(await read(client, mine, exact="true"), 454)["reconcile"] is None
    assert item_for(await read(client, theirs, exact="true"), 455)["reconcile"] is not None


async def test_re_reporting_is_idempotent_and_keeps_when_it_was_first_seen(client):
    """Two hosts run this timer and report the same pass minutes apart.

    `first_seen` surviving is also the only reason "a done candidate since
    Sunday" can be said at all — a pass holds no history, so the number exists
    here or nowhere. One of the three items this was written for had been closed
    for over two days.
    """
    repo = "acme/twice"
    await issue(client, repo, 456)

    await report(client, repo, [done(456)])
    first = item_for(await read(client, repo, exact="true"), 456)["reconcile"]

    again = await report(client, repo, [done(456, "still closed")], headers=DESKTOP)
    assert again["stored"] == 1 and again["resolved"] == 0
    second = item_for(await read(client, repo, exact="true"), 456)["reconcile"]

    assert second["since"] == first["since"], "the clock restarted on a repeat report"
    assert second["last_seen"] >= first["last_seen"]
    assert second["said"] == "still closed"
    assert second["reported_by"] != first["reported_by"], "the newest reporter is named"


async def test_a_pass_naming_one_ref_twice_keeps_the_last_and_does_not_refuse(client):
    """Two conditions can be true of one PR, and losing the report is the worse trade."""
    repo = "acme/twicenamed"
    await issue(client, repo, 457)

    out = await report(client, repo, [
        {"ref_kind": "issue", "ref_value": "457", "condition": "stale_claim"},
        done(457, "and also closed"),
    ])

    assert out["stored"] == 1
    found = item_for(await read(client, repo, exact="true"), 457)["reconcile"]
    assert found["condition"] == "done_candidate" and found["said"] == "and also closed"


# --------------------------------------------------------------- it decides nothing


async def test_a_flagged_item_is_not_touched(client):
    """No state transition, no reordering, no claim — the plan is still what somebody set.

    `qb-reconcile`'s refusal to write is right about the conditions that are
    decisions, and this endpoint inherits that refusal rather than routing around
    it. What was missing was never the decision; it was that the two facts never
    met.
    """
    repo = "acme/untouched-item"
    before = await issue(client, repo, 458)

    await report(client, repo, [done(458)])
    after = item_for(await read(client, repo, exact="true"), 458)

    for field in ("state", "rank", "rank_source", "note", "claim", "done", "done_by"):
        assert after[field] == before[field], f"{field} changed"
    assert after["state"] == "open"


async def test_an_item_with_no_ref_is_never_flagged(client):
    """There is nothing to reconcile a title against, and no key to do it with."""
    repo = "acme/refless"
    await add(client, repo, "house work, no forge behind it")

    await report(client, repo, [done(459)])
    plan = await read(client, repo, exact="true")

    assert plan["items"][0]["reconcile"] is None
    assert plan["next"]["caveat"] is None or "reconcile" not in plan["next"]["caveat"]
