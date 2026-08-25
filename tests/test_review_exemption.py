"""Exempting a PR from review is a human write — #335.

#273's review queue lets a PR leave the backlog three ways: merged, closed, or
exempted by the plan. PR #334 built the reader carefully — the drainer reads the
exemption and never writes one — and then left the marker in a plan item's
``note``, on ``POST /plan/item``, which every agent may call. So the worker could
authorise skipping the check that is on it. Not by defeating a check: by using
the documented API exactly as intended. #334's author named the position plainly:
*"visible and ageing today; not refused."*

That is the argument #85 and #86 each settled about ``require_human_triage`` and
#78 about ``judge_model``, one level further out — and the sharpest instance of
it, because those two govern whether work *starts* and this one governs whether
work is *inspected before it lands*. #85's sentence needs no editing to apply:
**the label that authorises work has to come from someone who is not the
worker.**

So the properties here are two, and the second matters as much as the first:

* **Refused.** No agent-writable path can put the marker on an open plan item for
  a PR. ``POST /plan/item``, ``POST /plan/submit`` and the completion note on
  ``POST /plan/item/done`` each refuse it, and the two paths that accept it —
  ``POST /plan/item/update`` and this endpoint's grant half — both take
  :func:`app.auth.human`.
* **Not a dead end.** An agent may still ASK. The request is durable (a line on
  the item), attributed, announced to a person on the board as #274's ``stuck``
  post, and idempotent. A refusal with nowhere for the request to go is one
  agents route around, and this repo has already counted what that costs (#274:
  four escalation doors, none of them the board, ``deferred: 0`` over thirty
  days).

And one property that is easy to get backwards: **a pending request changes
nothing about the queue.** The PR stays in it and stays drainable. A request that
held its own PR out of review would hand the worker, by a longer route, exactly
the authority the refusal withholds.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.db import async_session
from app.models.plan_item import PlanItem
from app.models.post import Post
from app.review_queue import (
    EXEMPT_MARKER,
    EXEMPT_REQUEST_MARKER,
    MarkerInReason,
    exempt_requested,
    exempting,
    grant_line,
    granted_exemption,
    request_line,
    requested_exemption,
    strip_exemption_lines,
)

from .conftest import LAPTOP, PINNED_SETTINGS

REPO = "acme/exemptrepo"
AGENT = {**LAPTOP, "X-Agent-Instance": "e17771"}
OTHER = {**LAPTOP, "X-Agent-Instance": "e17772"}
#: A person, as the edge proves one: the identity AND the secret only the proxy
#: knows. `Remote-User` alone is what any caller can send.
HUMAN = {"Remote-User": "rich", "X-Edge-Auth": PINNED_SETTINGS["HUMAN_EDGE_SECRET"]}
#: What an agent can trivially forge, and what must not be believed.
SPOOFED = {"Remote-User": "rich"}

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
EXEMPTING = "review: exempt — waiting on the upstream release"


# ----------------------------------------------------------------- vocabulary
# No database anywhere near these: whether a token exempts is a property of the
# token, and the whole change rests on one pattern not matching another.


def test_a_request_is_not_an_exemption_to_anything_that_greps_the_note():
    """The load-bearing property, asserted rather than assumed.

    The request marker is a hyphenated extension of the exemption marker, so the
    two live one character apart in a field that is free text. If
    `EXEMPT_MARKER`'s trailing lookahead ever stopped refusing the hyphen, every
    request would silently become the exemption it was written to ask for — the
    exact failure this issue exists to close, arriving through the fix.
    """
    asked = request_line("zeus/jasper-moss", "the diff is a changelog fragment")
    assert EXEMPT_REQUEST_MARKER.search(asked)
    assert EXEMPT_MARKER.search(asked) is None
    assert exempting(asked) is False
    assert exempt_requested(asked) is True


def test_a_granted_exemption_exempts_and_names_the_person_who_granted_it():
    line = grant_line("human/rich", "the upstream release is what we are waiting on")
    assert exempting(line) is True
    assert exempt_requested(line) is False
    got = granted_exemption(line)
    assert got is not None and got.by == "human/rich"
    assert got.reason == "the upstream release is what we are waiting on"


def test_a_request_round_trips_through_the_note_it_is_written_into():
    asked = request_line("zeus/jasper-moss", "no code in this diff at all")
    got = requested_exemption(f"the plan's own reasoning\n{asked}\nand more prose")
    assert got is not None
    assert (got.by, got.reason) == ("zeus/jasper-moss", "no code in this diff at all")


def test_a_reason_with_newlines_in_it_cannot_split_itself_across_the_marker():
    """A marker is line-oriented, so half a reason on an unread line is a reason
    half of which nothing can ever remove."""
    asked = request_line("zeus/jasper-moss", "one\nreason\n\nsplit over lines")
    assert "\n" not in asked
    assert requested_exemption(asked).reason == "one reason split over lines"


def test_the_marker_typed_by_hand_is_still_a_request_with_an_unknown_author():
    """Reported, not hidden. The marker is what a reader acts on, and an
    unattributable one is worth MORE attention than an attributed one."""
    got = requested_exemption("review: exempt-requested")
    assert got is not None and (got.by, got.reason) == (None, None)


@pytest.mark.parametrize("note, left, dropped", [
    ("keep me\nreview: exempt — go\ntail", "keep me\ntail", 1),
    ("keep me\nreview: exempt-requested by a — why", "keep me", 1),
    ("review: exempt", None, 1),
    ("nothing to strip", "nothing to strip", 0),
    (None, None, 0),
])
def test_removing_an_exemption_takes_the_whole_line_and_leaves_the_reasoning(
        note, left, dropped):
    assert strip_exemption_lines(note) == (left, dropped)


# ------------------------------------------------------------------- refused


async def add_pr_item(client, pr: int, headers=AGENT, **over) -> dict:
    r = await client.post("/plan/item", headers=headers, json={
        "title": f"PR {pr}", "repo": REPO, "ref_kind": "pr",
        "ref_value": str(pr), **over})
    assert r.status_code == 200, r.text
    return r.json()


async def snapshot(client, *numbers: int, headers=AGENT) -> dict:
    prs = [{"number": n, "headRefOid": SHA, "mergeable": "MERGEABLE",
            "title": f"pr {n}", "isDraft": False} for n in numbers]
    r = await client.post("/review-queue", json={"repo": REPO, "prs": prs},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def entry(queue: dict, number: int) -> dict:
    return next(e for e in queue["entries"] if e["pr"] == number)


async def test_an_agent_cannot_add_a_plan_item_that_exempts_its_own_pr(client):
    """The hole, closed at the endpoint that opened it.

    Before this change the same call answered 200 and the PR left the queue.
    """
    r = await client.post("/plan/item", headers=AGENT, json={
        "title": "PR 8100 is parked", "repo": REPO, "ref_kind": "pr",
        "ref_value": "8100", "note": EXEMPTING})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "human write" in detail["error"]
    # A refusal has to say where the request goes, or it is one agents route around.
    assert detail["propose"] == "POST /plan/item/exempt"

    # And nothing was written: the PR is in the queue, with no plan item at all.
    e = entry(await snapshot(client, 8100), 8100)
    assert e["state"] == "unreviewed" and e["drainable"] is True
    assert e["plan_item"] is None and e["exemption"] is None


async def test_the_same_note_on_an_issue_item_exempts_nothing_and_is_allowed(client):
    """Scoped to where the marker bites. The queue reads the marker off a PR's
    item and nothing else, so refusing it on an issue would make writing ABOUT
    this feature harder than using it — and buy no safety."""
    r = await client.post("/plan/item", headers=AGENT, json={
        "title": "the review: exempt marker", "repo": REPO, "ref_kind": "issue",
        "ref_value": "8101", "note": EXEMPTING})
    assert r.status_code == 200, r.text


async def test_a_submitted_plan_cannot_smuggle_an_exemption_past_the_refusal(client):
    """`POST /plan/submit` is the other agent-writable note, and it commits as a
    unit — so the refusal has to fire before anything is written, not after."""
    r = await client.post("/plan/submit", headers=AGENT, json={
        "repo": REPO, "label": "exemption smuggling", "claim": False, "items": [
            {"title": "something real"},
            {"title": "PR 8110", "ref_kind": "pr", "ref_value": "8110",
             "note": EXEMPTING},
        ]})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["item"] == 2

    # All-or-nothing: the innocent first item is not in the plan either.
    async with async_session() as s:
        rows = (await s.scalars(select(PlanItem).where(PlanItem.repo == REPO,
                                                       PlanItem.title == "something real"))
                ).all()
    assert list(rows) == []


async def test_a_completion_note_cannot_carry_an_exemption_either(client):
    """`done` appends an agent's words to a human-owned note. A done item exempts
    nothing today — the queue reads open items — but a person may reopen one, and
    an exemption that arrived through a receipt is one nobody decided."""
    item = await add_pr_item(client, 8120)
    r = await client.post("/plan/item/done", headers=AGENT, json={
        "item_id": item["item_id"], "note": EXEMPTING})
    assert r.status_code == 403, r.text
    assert "human write" in r.json()["detail"]["error"]


async def test_a_person_may_still_write_the_marker_the_way_they_always_could(client):
    """The other side of the refusal. `POST /plan/item/update` was already
    human-only, and after this change it is one of exactly two doors the marker
    can come through."""
    item = await add_pr_item(client, 8130)
    r = await client.post("/plan/item/update", headers=HUMAN, json={
        "item_id": item["item_id"], "note": EXEMPTING})
    assert r.status_code == 200, r.text

    e = entry(await snapshot(client, 8130), 8130)
    assert e["state"] == "exempt" and e["drainable"] is False
    # Typed by hand, so it exempts but says nothing about who granted it.
    assert e["plan_item"]["granted_by"] is None


async def test_a_forged_remote_user_buys_a_request_and_not_an_exemption(client):
    """`Remote-User` is client-settable, and the deployment note that says the
    edge strips it is a promise made in a config file this repo does not ship —
    so a header without the edge's secret beside it proves nothing here either.
    An agent that sends one is still an agent, and its call is still a request."""
    item = await add_pr_item(client, 8140)
    r = await client.post("/plan/item/exempt", headers={**AGENT, **SPOOFED}, json={
        "item_id": item["item_id"], "reason": "trust me", "grant": True})
    assert r.status_code == 200, r.text
    assert r.json()["exempted"] is False and r.json()["proposed"] is True
    assert r.json()["by"] == r.json()["requested"]["by"] != "human/rich"

    r = await client.post("/plan/item/update", headers={**AGENT, **SPOOFED}, json={
        "item_id": item["item_id"], "note": EXEMPTING})
    assert r.status_code == 403, r.text


async def test_the_grant_half_proves_a_person_the_way_the_live_reorder_does(client):
    """The deployment evidence for this endpoint, made transferable.

    ``POST /plan/reorder`` is known to work in production — Rich reordered a
    scope from a phone on 2026-08-23, and only a person can — which proves the
    edge is asserting ``X-Edge-Auth`` and the app accepting it. That proof
    transfers to the grant half only if the same credential is a person at both,
    so this asserts it directly rather than by reading two docstrings: one set of
    headers is a person at both endpoints, and one is a person at neither.

    The two are not identical in every respect — ``human()`` lets
    ``BROWSER_DEV_HUMAN`` outrank a bearer token and ``author()`` does not, which
    makes this endpoint the stricter of the pair. That flag is off here, off by
    default, and required unset in production by DEPLOY.md's checklist.
    """
    item = await add_pr_item(client, 8150)
    order = {"repo": REPO, "order": [item["item_id"]]}

    # The credential that works in production works at both.
    assert (await client.post("/plan/reorder", headers=HUMAN, json=order)
            ).status_code == 200
    granted = await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "parked"})
    assert granted.status_code == 200, granted.text
    assert granted.json()["granted"]["by"] == "human/rich"

    # And an agent's token is a person at neither: refused there, a request here.
    assert (await client.post("/plan/reorder", headers=AGENT, json=order)
            ).status_code == 403
    asked = await client.post("/plan/item/exempt", headers=OTHER, json={
        "item_id": item["item_id"], "reason": "me too"})
    assert asked.status_code == 200 and asked.json()["acted"] is False


# ------------------------------------------------------------- propose


async def test_an_agent_asks_and_the_request_is_durable_attributed_and_announced(client):
    item = await add_pr_item(client, 8200)
    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"],
        "reason": "the diff is one changelog fragment and no code"})
    assert r.status_code == 200, r.text
    got = r.json()

    # Asked for, not granted — and the response says so in the field a caller reads.
    assert got["exempted"] is False
    assert got["proposed"] is True and got["acted"] is True
    assert got["requested"]["by"].startswith("zeus/") or "/" in got["requested"]["by"]
    assert got["requested"]["reason"] == "the diff is one changelog fragment and no code"

    # Durable: it is on the item, so it survives the board being restarted and it
    # ages with `updated_at` like every other plan fact.
    assert exempt_requested(got["item"]["note"])
    assert exempting(got["item"]["note"]) is False

    # Announced: #274's one door — a `stuck` post, addressed to a person.
    async with async_session() as s:
        post = await s.get(Post, got["post"])
    assert post is not None
    assert post.type == "stuck" and post.recipient == "human"
    assert "needs a human (decision)" in post.summary
    assert "8200" in post.summary
    assert "changelog fragment" in (post.detail or "")
    assert {"kind": "pr", "value": "8200", "repo": REPO} in (post.refs or [])


async def test_a_pending_request_does_not_take_the_pr_out_of_the_queue(client):
    """The property that is easy to get backwards, and the one worth a test.

    If asking held a PR out of review, an agent could suspend its own review
    indefinitely by asking — the authority this endpoint withholds, reached by a
    longer route.
    """
    item = await add_pr_item(client, 8210)
    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "docs only"})
    assert r.status_code == 200, r.text

    e = entry(await snapshot(client, 8210), 8210)
    assert e["state"] == "unreviewed"
    assert e["drainable"] is True
    assert e["exemption"] is None
    assert e["plan_item"]["exempts"] is False
    # Visible, though — a person can see one waiting, and it ages.
    assert e["plan_item"]["exemption_requested"] is True
    assert e["exemption_requested"]["reason"] == "docs only"
    assert e["exemption_requested"]["by"]
    assert e["exemption_requested"]["since"]


async def test_asking_twice_is_one_request_and_one_message_to_read(client):
    item = await add_pr_item(client, 8220)
    first = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "docs only"})
    assert first.status_code == 200, first.text

    again = await client.post("/plan/item/exempt", headers=OTHER, json={
        "item_id": item["item_id"], "reason": "docs only, honestly"})
    assert again.status_code == 200, again.text
    assert again.json()["acted"] is False
    assert again.json()["proposed"] is False and again.json()["post"] is None
    # The same shape as the acting path: a retry has to be able to read the reply
    # it gets, and the retry is what this branch is for.
    assert again.json()["item"]["item_id"] == item["item_id"]
    # The original request stands; a second agent does not overwrite the first.
    assert again.json()["requested"]["reason"] == "docs only"

    async with async_session() as s:
        n = len((await s.scalars(
            select(Post).where(Post.type == "stuck", Post.recipient == "human"),
        )).all())
    stacked = again.json()["requested"]
    assert stacked is not None and n >= 1


async def test_a_person_disposes_and_the_pr_leaves_the_queue(client):
    item = await add_pr_item(client, 8230)
    asked = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "one changelog fragment"})
    assert asked.status_code == 200, asked.text

    r = await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "agreed — no code in it"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["exempted"] is True
    assert got["granted"]["by"] == "human/rich"
    assert got["requested"] is None          # the ask is answered, not stacked
    assert got["proposed"] is False

    e = entry(await snapshot(client, 8230), 8230)
    assert e["state"] == "exempt" and e["drainable"] is False
    assert e["next_action"] == "none"
    assert e["plan_item"]["granted_by"] == "human/rich"
    assert e["plan_item"]["exemption_requested"] is False
    assert "no code in it" in e["exemption"]["note"]


async def test_a_person_can_decline_and_the_items_own_reasoning_survives(client):
    item = await add_pr_item(client, 8240, note="ranked here because #300 needs it")
    await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "docs only"})

    r = await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "no — it touches auth", "grant": False})
    assert r.status_code == 200, r.text
    assert r.json()["exempted"] is False and r.json()["requested"] is None
    assert r.json()["item"]["note"] == "ranked here because #300 needs it"


async def test_a_person_can_revoke_an_exemption_they_granted(client):
    item = await add_pr_item(client, 8250)
    await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "parked"})
    assert entry(await snapshot(client, 8250), 8250)["state"] == "exempt"

    r = await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "changed my mind", "grant": False})
    assert r.status_code == 200, r.text
    assert entry(await snapshot(client, 8250), 8250)["state"] == "unreviewed"


async def test_an_agent_may_withdraw_its_own_request_but_not_a_persons_grant(client):
    item = await add_pr_item(client, 8260)
    await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "docs only"})

    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "never mind, it does touch code",
        "grant": False})
    assert r.status_code == 200, r.text
    assert r.json()["requested"] is None

    await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "parked"})
    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "putting it back", "grant": False})
    assert r.status_code == 403, r.text
    assert "may not revoke" in r.json()["detail"]["error"]


async def test_an_agent_asking_where_a_person_already_granted_changes_nothing(client):
    item = await add_pr_item(client, 8270)
    await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "parked"})

    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "please"})
    assert r.status_code == 200, r.text
    assert r.json()["exempted"] is True and r.json()["acted"] is False
    assert r.json()["proposed"] is False


async def test_a_pr_with_no_plan_item_is_in_the_queue_and_cannot_be_asked_about(client):
    """Silence is not exemption (#273), so there is nothing here to exempt — and
    the refusal says how to make the item rather than inventing one nobody
    ranked as a side effect of asking to skip its review."""
    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "repo": REPO, "pr": "8280", "reason": "docs only"})
    assert r.status_code == 404, r.text
    assert "silence is not exemption" in r.json()["detail"]["hint"]


async def test_an_item_can_be_named_by_the_pr_it_is_about(client):
    await add_pr_item(client, 8290)
    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "repo": REPO, "pr": "#8290", "reason": "docs only"})
    assert r.status_code == 200, r.text
    assert r.json()["pr"] == "8290"


async def test_an_exemption_on_something_that_is_not_a_pr_is_refused(client):
    r = await client.post("/plan/item", headers=AGENT, json={
        "title": "issue 8300", "repo": REPO, "ref_kind": "issue",
        "ref_value": "8300"})
    assert r.status_code == 200, r.text
    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": r.json()["item_id"], "reason": "docs only"})
    assert r.status_code == 422, r.text
    assert "not a pr" in r.json()["detail"]["error"]


async def test_a_flag_costs_a_reason_here_too(client):
    """#279's rule, and #67's: an escalation with nothing behind it is a
    confident assertion that costs somebody an interruption."""
    item = await add_pr_item(client, 8310)
    for reason in ("", "   "):
        r = await client.post("/plan/item/exempt", headers=AGENT, json={
            "item_id": item["item_id"], "reason": reason})
        assert r.status_code == 422, r.text


async def test_a_done_item_exempts_nothing_and_cannot_be_asked_about(client):
    item = await add_pr_item(client, 8320)
    r = await client.post("/plan/item/done", headers=AGENT, json={
        "item_id": item["item_id"], "note": "landed"})
    assert r.status_code == 200, r.text
    r = await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "parked"})
    assert r.status_code == 409, r.text


# ---------------------------------------- what the codex second opinion found
# Three defects, all of them in the propose path rather than the refusal — which
# is the shape worth remembering: the refusal was the part under scrutiny, and
# the way past it was the door built beside it.


def test_a_reason_may_not_be_the_marker_itself():
    """The bypass, at the unit that would have written it.

    `EXEMPT_MARKER` is unanchored and the request line ends with the reason, so a
    reason of literally `review: exempt` would end the line with a live marker —
    and an agent asking politely for an exemption would have granted itself one
    through the endpoint built to stop it. Silently, and past every test that
    only watched the endpoint agents used to call.
    """
    for smuggled in ("review: exempt", "review:exempt", "sure — REVIEW : EXEMPT ok",
                     "review: exempt-requested"):
        with pytest.raises(MarkerInReason):
            request_line("zeus/jasper-moss", smuggled)
        with pytest.raises(MarkerInReason):
            grant_line("human/rich", smuggled)


async def test_asking_with_the_marker_as_the_reason_is_refused_not_granted(client):
    item = await add_pr_item(client, 8400)
    r = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "review: exempt"})
    assert r.status_code == 422, r.text

    e = entry(await snapshot(client, 8400), 8400)
    assert e["state"] == "unreviewed" and e["drainable"] is True
    assert e["exemption"] is None


async def test_one_agent_cannot_clear_another_agents_request(client):
    """Withdraw-and-re-ask is the one loop that gets past "already asked", so
    leaving it open to anybody would make a person's notification queue writable
    by every agent on the board."""
    item = await add_pr_item(client, 8410)
    await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "docs only"})

    r = await client.post("/plan/item/exempt", headers=OTHER, json={
        "item_id": item["item_id"], "reason": "clearing this", "grant": False})
    assert r.status_code == 403, r.text
    assert "not yours to withdraw" in r.json()["detail"]["error"]

    # A person may, though — declining is what the request is FOR.
    r = await client.post("/plan/item/exempt", headers=HUMAN, json={
        "item_id": item["item_id"], "reason": "no", "grant": False})
    assert r.status_code == 200, r.text


async def test_re_asking_after_a_withdrawal_records_it_without_a_second_message(client):
    """The request survives; the interruption does not repeat. `announced: false`
    is the difference, said out loud, because a caller that cannot tell "recorded
    but quiet" from "failed" will re-ask to make sure."""
    item = await add_pr_item(client, 8420)
    first = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "docs only"})
    assert first.json()["announced"] is True and first.json()["post"]

    await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "never mind", "grant": False})
    again = await client.post("/plan/item/exempt", headers=AGENT, json={
        "item_id": item["item_id"], "reason": "docs only after all"})
    assert again.status_code == 200, again.text
    assert again.json()["proposed"] is True        # the request IS recorded
    assert again.json()["announced"] is False      # the person is not told twice
    assert again.json()["post"] is None
    assert again.json()["requested"]["reason"] == "docs only after all"


async def test_two_agents_asking_at_once_leave_one_request_and_one_message(client):
    """Everything here is read-modify-write over one free-text column, so the row
    is locked. Without the lock both callers see no request, both write one, and
    both interrupt a person about the same PR."""
    item = await add_pr_item(client, 8430)
    both = await asyncio.gather(*[
        client.post("/plan/item/exempt", headers=h, json={
            "item_id": item["item_id"], "reason": f"docs only ({n})"})
        for n, h in enumerate((AGENT, OTHER))])
    assert [r.status_code for r in both] == [200, 200], [r.text for r in both]
    assert sorted(r.json()["acted"] for r in both) == [False, True]
    assert len([r for r in both if r.json()["post"]]) == 1

    async with async_session() as s:
        note = (await s.get(PlanItem, uuid.UUID(item["item_id"]))).note
    assert note.count("review: exempt-requested") == 1


async def test_an_agent_racing_a_person_cannot_strip_the_grant_back_off(client):
    """The dangerous direction of the same race: a proposal that read the note
    before a person granted would write `strip(old) + request` over the grant,
    and an agent would have undone a human decision by racing it."""
    item = await add_pr_item(client, 8440)
    both = await asyncio.gather(
        client.post("/plan/item/exempt", headers=HUMAN, json={
            "item_id": item["item_id"], "reason": "parked, it is docs"}),
        client.post("/plan/item/exempt", headers=AGENT, json={
            "item_id": item["item_id"], "reason": "docs only"}),
    )
    assert [r.status_code for r in both] == [200, 200], [r.text for r in both]

    e = entry(await snapshot(client, 8440), 8440)
    assert e["state"] == "exempt", "the person's grant did not survive the race"
    assert e["plan_item"]["granted_by"] == "human/rich"


async def test_a_request_nobody_can_attribute_is_still_shown_as_one(client):
    """A marker typed by hand carries no name and no reason, and a page keying its
    chip off the author would render nothing at all for the one request nobody can
    attribute — the direction that loses information. So `GET /plan` publishes the
    flag as well as the name."""
    item = await add_pr_item(client, 8450)
    r = await client.post("/plan/item/update", headers=HUMAN, json={
        "item_id": item["item_id"], "note": "review: exempt-requested"})
    assert r.status_code == 200, r.text
    assert r.json()["review"] == {
        "exempt": False, "granted_by": None,
        "requested": True, "requested_by": None, "requested_reason": None}

    e = entry(await snapshot(client, 8450), 8450)
    assert e["plan_item"]["exemption_requested"] is True
    assert e["exemption_requested"]["by"] is None
    assert e["drainable"] is True


# --------------------------------------------- the delegated credential (#478)


async def test_a_delegated_credential_does_not_grant_an_exemption(client):
    """#335: an agent must not exempt its own PR. The delegated credential of #478
    must not become the longer route to doing it.

    Lives here rather than beside the other delegation tests for two reasons. This
    endpoint ANNOUNCES on the board, and a post addressed to `human` reaches every
    person hierarchically — so running it from a file that sorts before
    `test_human_board_writes.py` puts a post in an inbox that test asserts is
    honestly empty. And the item has to reference a **PR**: an exemption on an
    issue-backed item is refused on shape (*"names nothing, not a pr"*), which is a
    4xx that says nothing about who may grant one, so the easy version of this test
    passes against a board with the gate removed.
    """
    from .conftest import LAPTOP_ELEVATED
    repo = "acme/delegated-exempt"
    r = await client.post("/plan/item",
                          json={"title": "a pr", "repo": repo,
                                "ref_kind": "pr", "ref_value": "4242"},
                          headers=LAPTOP)
    assert r.status_code in (200, 201), r.text
    item = r.json()["item_id"]

    r = await client.post("/plan/item/exempt",
                          json={"item_id": item, "reason": "test", "grant": True},
                          headers={**LAPTOP_ELEVATED, "X-Agent-Instance": "e17999"})
    # NOT a refusal, and the 200 is the endpoint working rather than the gate
    # failing: `exempt` takes `author`, and "the same call an agent makes to ask is
    # the call a person makes to answer". The agent's `grant: true` is DOWNGRADED
    # to a request. What must hold is that nothing was actually exempted.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exempted"] is False, "the delegated credential granted an exemption"
    assert body["granted"] is None
    assert body["proposed"] is True, "it should have been recorded as a request"
