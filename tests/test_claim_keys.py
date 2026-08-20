"""#172: the key is derived, never composed — and the plan and the claims agree.

`claims()` returned `[]` fleet-wide for four months while thirteen agents worked
three shared checkouts. Two halves to that, and this file is the second one.

The first half is that nothing automatic wrote a claim. The second is that the
claims which DID exist were useless, because two agents describing one collision
produced two keys. The evidence, recorded on the issue at 22:59 on 2026-08-17:

    `claims()` showed `zeus/lantern-fennel` holding
    `kind=issue key=prisonblues/quarterback#163`, acquired 22:31, live and
    unexpired. The plan item referencing issue 163 read `"claim": null`, and the
    plan's own `counts` reported `"claimed": 0`. Same issue, same repo, same
    second, two answers.

Both subsystems were correct about their own string. `(kind, key)` is the unique
index, so `issue/<repo>#163` and `work/<repo>#163` are two resources by
construction — and nothing checked that the two agreed, because agreeing by
convention is not a thing that can be checked.

So the properties under test:

* **One resource, one key, whoever asks.** Derived from a ref, canonicalised from
  a composed pair, and the plan router's own key — all the same string.
* **A claim taken by hand shows up in the plan.** That is the join that was
  missing, and it is the whole reason the derivation matters.
* **An unrecognised key is left alone.** The rejected repair for #148 was a
  parser over an open domain; canonicalisation here recognises a closed set of
  shapes and touches nothing else.
* **A PR and an issue with the same number are not the same resource.** They were
  going to be, under a naive `kind` fold.
* **"Am I holding anything here" is one deterministic answer**, not a list three
  callers each re-derive the repo from.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.api.plan import claim_key, plan_claim_key
from app.claimkey import MERGE, WORK, BadRef, canonical, derive, repo_of
from app.db import async_session
from app.models.plan import Plan
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease

from .conftest import DESKTOP, LAPTOP

REPO = "acme/keyed"


async def claim_ref(client, kind: str, value: str, repo: str | None = REPO,
                    headers=LAPTOP, **over):
    ref = {"kind": kind, "value": value}
    if repo is not None:
        ref["repo"] = repo
    return await client.post("/claim", json={"ref": ref, **over}, headers=headers)


async def compose(client, kind: str, key: str, headers=LAPTOP, **over):
    return await client.post("/claim", json={"kind": kind, "key": key, **over},
                             headers=headers)


# ------------------------------------------------------- one resource, one key

def test_every_spelling_of_one_issue_derives_the_same_key():
    """The three producers that used to be three implementations: a ref, a
    composed pair, and the plan router's own idea of the key."""
    derived = derive("issue", repo="Acme/Keyed", value="#163")
    assert derived == (WORK, "acme/keyed#163")
    assert canonical("issue", "acme/keyed#163") == derived
    assert canonical("work", "Acme/Keyed#163") == derived
    assert canonical("task", "acme/keyed#163") == derived
    item = PlanItem(repo="acme/keyed", ref_kind="issue", ref_value="163",
                    title="t", rank=1, added_by="x")
    assert (WORK, claim_key(item)) == derived


def test_a_pr_and_an_issue_numbered_the_same_are_not_one_resource():
    """The trap in folding by `kind` alone. `#` was already the issue's — the
    plan, the dashboards' `issue_claims` join and every hand-taken claim use it —
    so the PR takes the new sigil, and `!` cannot occur in a GitHub owner,
    repository or branch name."""
    issue = derive("issue", repo=REPO, value="5")
    pr = derive("pr", repo=REPO, value="5")
    assert issue != pr
    assert pr == (WORK, "acme/keyed!5")
    # ...and a PR spelled with the issue sigil is still a PR: in a composed pair
    # the kind is the only thing that can tell them apart, so it decides.
    assert canonical("pr", "acme/keyed#5") == pr


def test_a_branch_keeps_its_own_kind_and_its_own_case():
    """A merge claim is not folded into `work`: landing a branch and doing the
    issue behind it are two resources, held at different times, and
    `preland.check_merge_claim` reads this kind by name.

    Branch case is NOT folded, unlike the repo's. GitHub repository names are
    case-insensitive; git refs are not, so `main` and `Main` are two branches and
    folding them would let an agent landing one hold the claim on the other."""
    assert derive("branch", repo="Acme/Keyed", value="feat/x") == \
        (MERGE, "acme/keyed:feat/x")
    assert canonical("merge", "acme/keyed:Main") != canonical("merge", "acme/keyed:main")


def test_a_key_this_board_does_not_understand_is_left_exactly_as_it_arrived():
    """The counterweight to all of the above, and the reason PR #152 was closed.

    A real claim on this board reads
    `prisonblues/lexray:serving-row:32022R2554` — a database row. Reading
    `kind='work'` as licence to parse it as a branch would have renamed it to a
    claim on a branch called `serving-row:32022R2554`. Only the merge kinds fold
    onto a merge key."""
    key = "prisonblues/lexray:serving-row:32022R2554"
    assert canonical("work", key) == ("work", key)
    assert repo_of("work", key) is None


def test_a_ref_that_cannot_be_keyed_is_refused_rather_than_guessed_at():
    for bad in [("issue", "not-a-number"), ("issue", "0"), ("branch", "with space"),
                ("plan", "not-a-uuid"), ("nonsense", "1")]:
        with pytest.raises(BadRef):
            derive(bad[0], repo=REPO, value=bad[1])


# ------------------------------------------- the join that was missing (#172)

async def test_a_claim_taken_by_hand_is_visible_in_the_plan(client):
    """The bug, as a test. An agent claiming `kind='issue'` — the spelling agents
    actually used — was invisible to a plan filtering on `kind='work'`, so the
    plan reported `claimed: 0` about an issue somebody was holding."""
    repo = "acme/thejoin"
    added = await client.post("/plan/item", json={
        "title": "#163", "repo": repo, "ref_kind": "issue", "ref_value": "163"},
        headers=LAPTOP)
    assert added.status_code == 200, added.text

    taken = await compose(client, "issue", f"{repo}#163", note="landing it",
                          session="s-hand")
    assert taken.status_code == 200, taken.text
    # Canonicalised, and the caller is TOLD, so it cannot believe it holds a key
    # the board does not have.
    assert (taken.json()["kind"], taken.json()["key"]) == (WORK, f"{repo}#163")
    assert taken.json()["derived_from"] == {"kind": "issue", "key": f"{repo}#163"}

    plan = await client.get("/plan", params={"repo": repo}, headers=LAPTOP)
    item = plan.json()["items"][0]
    assert item["claim"] is not None, "the plan cannot see a claim on its own issue"
    assert item["claim"]["note"] == "landing it"
    assert plan.json()["counts"]["claimed"] == 1
    assert plan.json()["next"] is None, "held work must not be offered as next"


async def test_a_claim_by_ref_and_a_plan_claim_are_one_row(client):
    """The other direction: the plan takes the claim, and an agent asking about the
    issue by ref is refused rather than told it is free."""
    repo = "acme/refjoin"
    added = await client.post("/plan/item", json={
        "title": "#77", "repo": repo, "ref_kind": "issue", "ref_value": "77"},
        headers=LAPTOP)
    item_id = added.json()["item_id"]
    took = await client.post("/plan/item/claim",
                             json={"item_id": item_id, "session": "s-plan"},
                             headers=LAPTOP)
    assert took.status_code == 200, took.text

    refused = await claim_ref(client, "issue", "77", repo=repo, headers=DESKTOP)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["key"] == f"{repo}#77"

    found = await client.get("/claims", params={
        "ref_kind": "issue", "ref_value": "#77", "repo": repo}, headers=LAPTOP)
    assert [c["key"] for c in found.json()["claims"]] == [f"{repo}#77"]


async def test_a_lookup_by_the_old_spelling_still_finds_the_claim(client):
    """A caller that has not been updated asks `kind=issue`. Answering "nobody
    holds it" about a row that is right there is how the disagreement read from
    outside, so the read path canonicalises exactly as the write path does."""
    repo = "acme/oldlookup"
    assert (await claim_ref(client, "issue", "9", repo=repo)).status_code == 200
    r = await client.get("/claims", params={"kind": "issue", "key": f"{repo}#9"},
                         headers=LAPTOP)
    assert [c["key"] for c in r.json()["claims"]] == [f"{repo}#9"]


async def test_a_pr_backed_plan_item_joins_a_hand_taken_pr_claim(client):
    """Before #172 a PR-backed item fell back to its own id, so a PR claimed by
    hand was invisible to the plan. It is keyed now, and kept clear of the issue
    numbered the same."""
    repo = "acme/prjoin"
    added = await client.post("/plan/item", json={
        "title": "review !12", "repo": repo, "ref_kind": "pr", "ref_value": "12"},
        headers=LAPTOP)
    assert added.status_code == 200, added.text
    assert (await claim_ref(client, "pr", "12", repo=repo, note="reviewing")
            ).status_code == 200
    plan = await client.get("/plan", params={"repo": repo}, headers=LAPTOP)
    item = plan.json()["items"][0]
    assert item["claim"]["key"] == f"{repo}!12"


async def test_ref_and_composed_cannot_both_be_sent(client):
    """A request carrying both is a caller with two ideas about what it is
    claiming, and guessing which one it meant is how a claim lands on the wrong
    resource."""
    r = await client.post("/claim", json={
        "ref": {"kind": "issue", "repo": REPO, "value": "1"},
        "kind": "work", "key": f"{REPO}#2"}, headers=LAPTOP)
    assert r.status_code == 422, r.text
    empty = await client.post("/claim", json={"note": "nothing"}, headers=LAPTOP)
    assert empty.status_code == 422


# ------------------------------------------------ block on pickup: /claim/held

async def test_held_answers_yes_or_no_for_this_repo(client):
    """The deterministic boolean a pickup gate reads. Three callers re-deriving it
    from a claim list is three chances to attribute a key to the wrong repo — and
    the fleet has already spent an evening on exactly that."""
    mine, other = "acme/heldmine", "acme/heldother"
    assert (await claim_ref(client, "issue", "1", repo=mine, session="s-h")
            ).status_code == 200

    yes = await client.get("/claim/held", params={"repo": mine}, headers=LAPTOP)
    assert yes.json()["held"] is True
    assert [c["repo"] for c in yes.json()["claims"]] == [mine]

    no = await client.get("/claim/held", params={"repo": other}, headers=LAPTOP)
    assert no.json()["held"] is False and no.json()["claims"] == []

    # A different machine holds nothing, even in the repo where somebody does.
    theirs = await client.get("/claim/held", params={"repo": mine}, headers=DESKTOP)
    assert theirs.json()["held"] is False


async def test_held_defaults_to_the_CALLER_rather_than_taking_a_name(client):
    """An agent asking "am I holding anything" must not have to name itself: a
    client-supplied identity is how a co-tenant's claim comes back as your own."""
    repo = "acme/heldself"
    assert (await claim_ref(client, "issue", "2", repo=repo)).status_code == 200
    r = await client.get("/claim/held", params={"repo": repo}, headers=DESKTOP)
    assert r.json()["holder"].startswith("desktop")
    assert r.json()["held"] is False


async def test_a_key_with_no_repo_is_reported_rather_than_dropped(client):
    """"I am holding something and it does not say which repo" is a different
    answer from "I am holding nothing". A gate that collapsed them would stop an
    agent that is demonstrably working."""
    r = await client.post("/claim", json={
        "kind": "work", "key": "acme/opaque:serving-row:32022R2554",
        "session": "s-op"}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    held = await client.get("/claim/held", params={"repo": "acme/opaque"},
                            headers=LAPTOP)
    assert held.json()["held"] is False
    assert any(c["key"].endswith("32022R2554") for c in held.json()["unattributed"])


async def test_a_lapsed_claim_does_not_count_as_held(client):
    """Passive expiry, read through the gate: a crashed holder must not keep
    reading as "still working", or the gate lets it past forever."""
    repo = "acme/heldlapse"
    took = await claim_ref(client, "issue", "3", repo=repo, ttl=1, session="s-l")
    assert took.status_code == 200
    async with async_session() as s:
        await s.execute(update(ResourceLease)
                        .where(ResourceLease.key == f"{repo}#3")
                        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()
    r = await client.get("/claim/held", params={"repo": repo}, headers=LAPTOP)
    assert r.json()["held"] is False


def test_a_plan_key_names_no_repo_and_that_is_deliberate():
    """A plan may span repos — #172 closes on that as an open question, and the
    answer the schema gives is that a plan's scope is its own. So a plan claim is
    reported under `unattributed` rather than being attributed to a repo it may not
    belong to."""
    plan = Plan(id=uuid.uuid4(), label="l", added_by="x")
    assert repo_of(WORK, plan_claim_key(plan)) is None
