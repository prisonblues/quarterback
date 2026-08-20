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


# ------------------------------------------- whose claim, when the two clients
#                                             do not send the same headers


async def test_the_gate_sees_a_claim_taken_through_the_OTHER_client(client):
    """Round 2's F01. The two clients that make up #172's feature identify
    differently, and `/claim/held` used to compare holders with `==`.

    `mcp/mcp_server/client.py` sends `X-Agent-Key`, so an agent claiming through
    the MCP `claim` tool is written down as `laptop/<allocated-name>`.
    `harness/bin/qbdata.py` sends only `Authorization`, so `qb-claim` — and
    therefore `create-worktree` — writes under the bare `laptop`, and `qb-claimed`
    reads under the bare `laptop`. Under plain equality each was invisible to the
    other: the pickup gate reported `free` for an agent that had just claimed, and
    the tool reported `free` for the claim the checkout took on its behalf.

    Every other ownership test on this table goes through `same_machine` for
    exactly this reason, so this one now does too — via `address_clause`, the same
    clause `/active` already uses on `Lease.holder`.
    """
    repo = "acme/heldtwoclients"
    keyed = {**LAPTOP, "X-Agent-Key": "keyaaaa"}

    took = await claim_ref(client, "issue", "11", repo=repo, headers=keyed,
                           session="s-both")
    assert took.status_code == 200, took.text
    assert took.json()["holder"].startswith("laptop/"), (
        "the point of the test is that the writer got a NAME, not the bare machine")

    # ...and the bare-machine reader, which is the CLI half, can see it.
    seen = await client.get("/claim/held", params={"repo": repo, "session": "s-both"},
                            headers=LAPTOP)
    assert seen.status_code == 200, seen.text
    assert seen.json()["held"] is True
    assert [c["key"] for c in seen.json()["claims"]] == [f"{repo}#11"]

    # And the reverse direction: what the checkout took, the tool can see.
    bare = await claim_ref(client, "issue", "12", repo=repo, headers=LAPTOP)
    assert bare.status_code == 200, bare.text
    assert bare.json()["holder"] == "laptop"
    back = await client.get("/claim/held", params={"repo": repo}, headers=keyed)
    assert back.json()["held"] is True
    assert f"{repo}#12" in [c["key"] for c in back.json()["claims"]]


async def test_a_co_tenants_session_claim_is_still_not_yours(client):
    """Machine-scoped is not machine-wide. The session is what separates two
    agents on one box — `may_mutate`'s rule, read as a filter — so widening the
    holder match must not hand a co-tenant's work to whoever asks next."""
    repo = "acme/heldcotenant"
    one = {**LAPTOP, "X-Agent-Key": "keybbbb"}
    two = {**LAPTOP, "X-Agent-Key": "keycccc"}

    assert (await claim_ref(client, "issue", "21", repo=repo, headers=one,
                            session="s-one")).status_code == 200
    mine = await client.get("/claim/held", params={"repo": repo, "session": "s-two"},
                            headers=two)
    assert mine.json()["held"] is False, (
        "a co-tenant's session claim came back as this agent's own")


async def test_a_claim_that_named_no_session_belongs_to_the_machine(client):
    """The checkout claim, and why it records no session.

    `create-worktree` claims before the tree exists, so the agent that will work
    in the tree has no session yet. A claim that named none falls back to the
    machine — `may_mutate` says so in as many words — so the session that
    eventually picks it up has to be able to see it, or the gate stops the very
    agent the checkout claimed for.
    """
    repo = "acme/heldnosession"
    took = await claim_ref(client, "issue", "31", repo=repo, headers=LAPTOP)
    assert took.status_code == 200 and took.json()["session"] is None

    picked_up = await client.get(
        "/claim/held", params={"repo": repo, "session": "some-later-session"},
        headers={**LAPTOP, "X-Agent-Key": "keydddd"})
    assert picked_up.json()["held"] is True
    assert f"{repo}#31" in [c["key"] for c in picked_up.json()["claims"]]


async def test_an_EMPTY_session_is_no_session(client):
    """The wire form the checkout uses. `create-worktree` passes `--session ""` to
    override qb-claim's environment default, so the empty string has to arrive as
    "no session" rather than as a session nobody else can ever match — which is
    what `clean_session` is for, and this pins the one caller that depends on it.
    """
    took = await claim_ref(client, "issue", "61", repo="acme/heldemptysess",
                           session="")
    assert took.status_code == 200, took.text
    assert took.json()["session"] is None


async def test_another_machine_still_holds_nothing(client):
    """The widening is to the machine and stops there. `same_machine` is the
    authorisation boundary everywhere else on this table and it stays one here."""
    repo = "acme/heldothermachine"
    assert (await claim_ref(client, "issue", "41", repo=repo,
                            headers={**LAPTOP, "X-Agent-Key": "keyeeee"},
                            session="s-x")).status_code == 200
    theirs = await client.get("/claim/held", params={"repo": repo}, headers=DESKTOP)
    assert theirs.json()["held"] is False


# --------------------------------------- a plan or item claim IS work in a repo


async def _a_plan(repo: str | None, label: str) -> uuid.UUID:
    plan_id = uuid.uuid4()
    async with async_session() as s:
        s.add(Plan(id=plan_id, repo=repo, label=label, added_by="tester"))
        await s.commit()
    return plan_id


async def test_a_plan_claim_is_held_work_in_the_plans_own_repo(client):
    """Round 2's F02. `repo_of` cannot attribute `plan:<uuid>` and is right not to
    — an id says nothing about a repository. But the ROW does, and #172's whole
    design routes the fuzzy intake through a plan claim, so a gate blind to plan
    claims is blind to the intake the issue added: an agent holding the plan for
    this very repo was reported `unattributed`, and `held` came back false."""
    repo = "acme/heldplanscope"
    plan_id = await _a_plan(repo, "stage held")
    took = await claim_ref(client, "plan", str(plan_id), repo=None, session="s-plan")
    assert took.status_code == 200, took.text

    r = await client.get("/claim/held", params={"repo": repo, "session": "s-plan"},
                         headers=LAPTOP)
    assert r.json()["held"] is True
    assert [c["key"] for c in r.json()["claims"]] == [f"plan:{plan_id}"]
    assert [c["repo"] for c in r.json()["claims"]] == [repo]

    # ...and it is that repo, not every repo.
    elsewhere = await client.get(
        "/claim/held", params={"repo": "acme/heldplanelsewhere", "session": "s-plan"},
        headers=LAPTOP)
    assert elsewhere.json()["held"] is False


async def test_an_item_claim_is_held_work_in_the_items_own_repo(client):
    """The same join, one level down: a ref-less item has no issue key to be
    attributed by, so its own scope is the only thing that can say where it is."""
    repo = "acme/helditemscope"
    plan_id = await _a_plan(repo, "stage item")
    item_id = uuid.uuid4()
    async with async_session() as s:
        s.add(PlanItem(id=item_id, repo=repo, plan_id=plan_id, title="loose",
                       rank=1, added_by="tester"))
        await s.commit()
    assert (await claim_ref(client, "item", str(item_id), repo=None,
                            session="s-item")).status_code == 200
    r = await client.get("/claim/held", params={"repo": repo, "session": "s-item"},
                         headers=LAPTOP)
    assert r.json()["held"] is True
    assert [c["key"] for c in r.json()["claims"]] == [f"item:{item_id}"]


async def test_a_FLEET_plan_claim_is_still_unattributed(client):
    """A NULL scope really does not say which repo — that is the open question
    #172 closes on, and the schema's answer is that a fleet plan's items carry
    their own repo. So this stays `unattributed`: honest, and different from
    "holding nothing"."""
    plan_id = await _a_plan(None, f"fleet {uuid.uuid4().hex[:8]}")
    assert (await claim_ref(client, "plan", str(plan_id), repo=None,
                            session="s-fleet")).status_code == 200
    r = await client.get("/claim/held",
                         params={"repo": "acme/heldfleetasked", "session": "s-fleet"},
                         headers=LAPTOP)
    assert r.json()["held"] is False
    assert f"plan:{plan_id}" in [c["key"] for c in r.json()["unattributed"]]
    # Finish it. A fleet-scoped plan is in EVERY repo's widened read by design, so
    # an open one left here becomes a row in every later test's scope — the rule
    # `test_plan_items.py` states about its own fleet items, and this file is the
    # first to make a fleet PLAN.
    async with async_session() as s:
        await s.execute(update(Plan).where(Plan.id == plan_id)
                        .values(state="done", done_at=datetime.now(UTC)))
        await s.commit()


# ---------------------------------------------------- the read path's filters


async def test_a_kind_only_filter_finds_the_rows_it_is_asking_about(client):
    """Round 2's F09/F10. Every claim on a unit of work is stored under `work`
    now, so `?kind=issue` — which is what the pre-#172 vocabulary trained every
    agent, skill and dashboard to send — matched no row and answered
    `{"claims": []}` about resources that were held. That is this module's own
    named defect reproduced in the read path."""
    repo = "acme/kindonly"
    assert (await claim_ref(client, "issue", "51", repo=repo,
                            session="s-kind")).status_code == 200

    for spelling in ("issue", "task", "item", "epic", "work"):
        r = await client.get("/claims", params={"kind": spelling, "limit": 1000},
                             headers=LAPTOP)
        assert r.status_code == 200, r.text
        keys = [c["key"] for c in r.json()["claims"]]
        assert f"{repo}#51" in keys, f"?kind={spelling} lost a claim that is right there"

    # And the fold is REPORTED, because a kind alone can no longer tell an issue
    # from a PR and answering as if it could is the same silent wrong answer.
    folded = await client.get("/claims", params={"kind": "issue"}, headers=LAPTOP)
    assert folded.json()["filtered_on"] == {"kind": "work", "key": None}
    assert folded.json()["asked_for"] == {"kind": "issue", "key": None}
    assert "work" in folded.json()["note_on_kind"]

    plain = await client.get("/claims", params={"kind": "work"}, headers=LAPTOP)
    assert "note_on_kind" not in plain.json(), (
        "a kind that was already canonical was not folded, so there is nothing to say")


async def test_a_kind_only_filter_folds_the_merge_spellings_too(client):
    repo = "acme/kindmerge"
    assert (await claim_ref(client, "branch", "feat/kind-merge", repo=repo,
                            session="s-branch")).status_code == 200
    for spelling in ("branch", "land", "merge"):
        r = await client.get("/claims", params={"kind": spelling, "limit": 1000},
                             headers=LAPTOP)
        assert f"{repo}:feat/kind-merge" in [c["key"] for c in r.json()["claims"]], spelling


async def test_a_read_carrying_BOTH_spellings_is_refused(client):
    """Round 2's F08. `ClaimIn` refuses a write carrying both, on the grounds that
    "a request carrying both is a caller with two ideas about what it is
    claiming". The read took the opposite line and silently preferred `ref_kind`,
    so a caller with two ideas was answered about one of them and could not tell
    which. Same rule, both directions."""
    both = await client.get("/claims", params={
        "ref_kind": "issue", "ref_value": "1", "repo": REPO,
        "kind": "work", "key": f"{REPO}#2"}, headers=LAPTOP)
    assert both.status_code == 422, both.text
    assert "not both" in both.text

    lonely = await client.get("/claims", params={"ref_value": "1"}, headers=LAPTOP)
    assert lonely.status_code == 422
    assert "go together" in lonely.text


# ------------------------------------------------- the allocator really is gone


async def test_a_release_claim_cannot_be_taken_any_more(client):
    """Round 2's F14. #172 deleted the allocator because a stale record is worse
    than none — "a second answer to a question that has one". The endpoints went;
    `POST /claim {kind: 'release'}` did not, because canonicalisation passes an
    unrecognised kind through and the `RESERVED_KINDS` guard went with the
    allocator. A deletion that leaves one path able to write the rows is not a
    deletion."""
    r = await client.post("/claim", json={"kind": "release", "key": "acme/rel:2.31"},
                          headers=LAPTOP)
    assert r.status_code == 422, r.text
    assert "release_stamp" in r.text, "a refusal has to name what to do instead"

    # The refusal is in the primitive, so the read path says the same thing.
    q = await client.get("/claims", params={"kind": "release"}, headers=LAPTOP)
    assert q.status_code == 422 and "release_stamp" in q.text


async def test_a_LEGACY_release_row_does_not_break_the_gate(client):
    """The rows the allocator already wrote stay readable. Refusing the kind on
    the way in must not turn a row that is already there into a 500 on the way
    out — which is the whole reason `repo_of` folds without the refusal."""
    async with async_session() as s:
        s.add(ResourceLease(kind="release", key="acme/legacyrel:2.31",
                            holder="laptop", session="s-legacy",
                            ttl_seconds=3600,
                            expires_at=datetime.now(UTC) + timedelta(hours=1)))
        await s.commit()
    r = await client.get("/claim/held", params={"session": "s-legacy"}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert "acme/legacyrel:2.31" in [c["key"] for c in r.json()["unattributed"]]


# ------------------------------------- matching a shape is not being that shape


@pytest.mark.parametrize("kind,key,where", [
    # `_REPO_GROUP` admits a `.git` suffix; `REPO_RE` refuses it — so the repo
    # half is not a repo this board keys either, and the claim is unattributed.
    ("work", "acme/foo.git#12", None),
    # `_ISSUE_KEY` admits `#0`; issue numbers start at 1. The repo half is fine,
    # so the claim is still attributable — an unusable number does not move a
    # claim out of the repository its key names.
    ("work", "acme/foo#0", "acme/foo"),
    ("pr", "acme/foo!0", None),
    # `_UUID_KEY` admits 32-36 characters of hex and dashes that are not a uuid.
    ("work", "plan:----------------------------------", None),
    ("work", "item:0123456789abcdef0123456789abcdefff", None),
])
def test_a_key_that_matches_a_shape_without_being_one_passes_through(kind, key, where):
    """Round 2's F15. The key regexes are deliberately looser than the validators
    `derive` then applies, so `canonical` could raise out of a READ: one legacy
    row of this shape turned `GET /claim/held` into a 500 for every row, and
    `?kind=work&key=acme/foo%230` into a 500 for the caller.

    Rows of exactly this shape are reachable — `_norm_scope` used only to
    lower-case a repo, so an item scoped `acme/foo.git` stored the key
    `acme/foo.git#12`. The fallthrough is the module's own rule, not a new one: a
    key this board cannot key is left exactly as it arrived.
    """
    assert canonical(kind, key) == (kind, key)
    assert repo_of(kind, key) == where


async def test_a_malformed_key_lookup_answers_rather_than_500s(client):
    r = await client.get("/claims", params={"kind": "work", "key": "acme/foo.git#0"},
                         headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert r.json()["claims"] == []


async def test_a_legacy_dotgit_row_is_readable_through_the_gate(client):
    """The row `_norm_scope` used to be able to write, read back through the
    endpoint that iterates every row of a holder."""
    async with async_session() as s:
        s.add(ResourceLease(kind="work", key="acme/legacy.git#12", holder="laptop",
                            session="s-dotgit", ttl_seconds=3600,
                            expires_at=datetime.now(UTC) + timedelta(hours=1)))
        await s.commit()
    r = await client.get("/claim/held", params={"session": "s-dotgit"}, headers=LAPTOP)
    assert r.status_code == 200, r.text
    assert "acme/legacy.git#12" in [c["key"] for c in r.json()["unattributed"]]


# ------------------------------------------------------------ branch shape rules


@pytest.mark.parametrize("branch", [
    "feat/issue-172", "main", "Main", "release/2.58", "a-b_c.d", "fix/@thing",
])
def test_a_branch_name_git_accepts_is_a_key(branch):
    kind, key = derive("branch", repo="acme/br", value=branch)
    assert (kind, key) == (MERGE, f"acme/br:{branch}")


@pytest.mark.parametrize("branch", [
    "feat~1",            # git's ancestry operator
    "feat^",             # ditto
    "refs/heads:x",      # `:` — and `_MERGE_KEY` splits on the first one
    "what?",             # a pathspec wildcard
    "star*",
    "brack[et",
    "back\\slash",
    "a..b",              # the range operator
    "head@{1}",          # reflog syntax
    "@",
    "/leading",
    "trailing/",
    "trailing.",
    "feat/x.lock",
    "feat/.hidden",
    "double//slash",
    "with space",
    "with\ttab",
    "new\nline",
])
def test_a_branch_name_git_REFUSES_is_not_a_key(branch):
    """Round 2's F24. `_branch` rejected whitespace alone while claiming to reject
    git-reserved characters, so a merge key could name a ref that cannot exist —
    a claim two agents can never contend over, in the table that exists to make
    contention visible. The `:` case is worse than useless: `_MERGE_KEY` splits on
    the first colon after the repo, so such a key round-trips to a different
    branch name."""
    with pytest.raises(BadRef):
        derive("branch", repo="acme/br", value=branch)


@pytest.mark.parametrize("given,want", [("  main  ", "main"), ("\tmain\n", "main")])
def test_surrounding_whitespace_is_stripped_rather_than_refused(given, want):
    """The strip predates this and stays: a branch pasted with a newline on it is
    the branch, not a different one. Only whitespace INSIDE the name is a refusal,
    because there git has no such ref."""
    assert derive("branch", repo="acme/br", value=given) == (MERGE, f"acme/br:{want}")


def test_a_refused_branch_key_is_left_alone_rather_than_mangled():
    """`canonical` folds what it can and touches nothing else. A composed pair
    naming an impossible branch is not repaired into a possible one."""
    assert canonical("branch", "acme/br:feat~1") == ("branch", "acme/br:feat~1")


async def test_a_ref_naming_an_impossible_branch_is_a_422(client):
    r = await client.post("/claim", json={
        "ref": {"kind": "branch", "repo": "acme/br", "value": "feat~1"}},
        headers=LAPTOP)
    assert r.status_code == 422, r.text
    assert "check-ref-format" in r.text
