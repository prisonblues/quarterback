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


# ------------------------------------ #576: one row per QUESTION, not per class


async def test_two_conditions_on_one_subject_and_class_are_two_rows(client):
    """The defect #576 is filed about, measured on the live board before it was
    fixed: `qb-doctor` raised `landed`, `harness` and `unpushed` against one repo,
    all of them `environment`, and the table held ONE row. The second and third
    were answered "an open blocker already asks this of this subject" and thrown
    away, so the surface a person scans undercounted by two."""
    first = await raise_one(client, subject_kind="repo", subject_value=REPO,
                            kind="environment", condition="landed",
                            question="4 PRs ready and main has not moved")
    second = await raise_one(client, subject_kind="repo", subject_value=REPO,
                             kind="environment", condition="unpushed",
                             question="25 commits exist on no remote")
    assert first.json()["raised"] is True
    assert second.json()["raised"] is True, second.text
    assert first.json()["blocker"]["id"] != second.json()["blocker"]["id"]


async def test_the_same_condition_re_raised_is_still_a_no_op(client):
    """The other half of the boundary, and the one that must not have been broken
    to get the first. A loop asking the same question every run still gets the
    existing row back — a condition that moved with the READING rather than the
    fault would refill the table this index exists to keep small."""
    first = await raise_one(client, subject_kind="repo", subject_value=f"{REPO}-same",
                            kind="environment", condition="landed",
                            question="2 pull requests ready to land")
    again = await raise_one(client, subject_kind="repo", subject_value=f"{REPO}-same",
                            kind="environment", condition="landed",
                            question="4 pull requests ready to land")
    assert again.json()["raised"] is False
    assert again.json()["blocker"]["id"] == first.json()["blocker"]["id"]
    assert "landed" in again.json()["note"]


async def test_re_raising_one_of_several_conditions_returns_that_row_not_a_500(client):
    """The recovery after a unique violation re-reads "the row the collision
    names", and it has to key on what the INDEX keys on. Filtering on the old
    four-part key matches every condition open on the subject, so
    `scalar_one_or_none` raises `MultipleResultsFound` and the documented no-op
    becomes a 500 — in exactly the case the column was added for."""
    subject = f"{REPO}-many"
    made = {}
    for cond in ("landed", "harness@zeus", "unpushed"):
        r = await raise_one(client, subject_kind="repo", subject_value=subject,
                            kind="environment", condition=cond, question=f"{cond}?")
        assert r.json()["raised"] is True, r.text
        made[cond] = r.json()["blocker"]["id"]

    again = await raise_one(client, subject_kind="repo", subject_value=subject,
                            kind="environment", condition="harness@zeus",
                            question="8 scripts differ")
    assert again.status_code == 200, again.text
    assert again.json()["raised"] is False
    assert again.json()["blocker"]["id"] == made["harness@zeus"], \
        "the collision must return the row it actually collided with"


async def test_a_condition_is_trimmed_and_lowercased_before_it_keys_anything(client):
    """Otherwise `landed`, `landed ` and `Landed` are three standing questions
    about one fault. Normalised at the edge, the way every other value a consumer
    keys on is — and passed through otherwise, because the namespace is open and
    refusing an unfamiliar condition would refuse an escalation."""
    first = await raise_one(client, subject_kind="repo", subject_value=f"{REPO}-norm",
                            kind="environment", condition="landed")
    assert first.json()["blocker"]["condition"] == "landed"
    again = await raise_one(client, subject_kind="repo", subject_value=f"{REPO}-norm",
                            kind="environment", condition="  LANDED ")
    assert again.json()["raised"] is False
    assert again.json()["blocker"]["id"] == first.json()["blocker"]["id"]


async def test_no_condition_is_the_empty_string_and_dedupes_as_it_always_did(client):
    """Most producers pass none — `preland`, `panel`, `epic` and `issue_watch` all
    key on a real PR or issue and raise one question per class about it. Their
    behaviour is unchanged, which is what `NOT NULL DEFAULT ''` buys: nullable
    would have switched deduplication OFF for exactly them, because PostgreSQL
    treats NULLs in a unique index as distinct."""
    first = await raise_one(client, subject_value="i-plain")
    again = await raise_one(client, subject_value="i-plain")
    assert first.json()["blocker"]["condition"] == ""
    assert again.json()["raised"] is False


async def test_a_fleet_scope_question_is_deduplicated_too(client):
    """`repo` is nullable and NULL means fleet scope — a real value, not a missing
    one. Under PostgreSQL's default no NULL equals another, so this index never
    deduplicated fleet-scope rows at all and the `repo IS NULL` branch of the
    recovery could not run for want of a collision to recover from. An idempotency
    promise that holds for some rows and quietly not for others is worse than
    none, because the docstring is read as covering both."""
    first = await raise_one(client, repo=None, subject_value="i-fleet",
                            kind="decision")
    again = await raise_one(client, repo=None, subject_value="i-fleet",
                            kind="decision")
    assert first.json()["raised"] is True, first.text
    assert again.json()["raised"] is False, again.text
    assert again.json()["blocker"]["id"] == first.json()["blocker"]["id"]


async def test_a_condition_longer_than_the_table_takes_is_refused(client):
    """A bound the table enforces as well, so this cannot be the only thing
    standing between a producer and a CHECK violation. It is short because a
    condition is an identifier and not a sentence — anything near it is almost
    certainly a reading that has been mistaken for a fault."""
    r = await raise_one(client, condition="x" * 200)
    assert r.status_code == 422


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


# ------------------ a blocker that names the forge reaches the item that carries it


async def add_ref_item(client, title, repo, kind, value):
    r = await client.post("/plan/item", json={"title": title, "repo": repo,
                                              "ref_kind": kind, "ref_value": value},
                          headers=LAPTOP)
    assert r.status_code in (200, 201), r.text
    MINE.add(r.json()["item_id"])
    return r.json()["item_id"]


@pytest.mark.parametrize("kind", ["issue", "pr"])
async def test_next_skips_an_item_whose_ISSUE_OR_PR_is_blocked(client, kind):
    """#555, and the reason #328's queue could not partition anything.

    Every producer the fleet has raises the FORGE kind — `needs_human._subject_from`
    prefers `pr`, then `issue`, and records `item` as the kind "nothing emits today"
    — because a loop reviewing a pull request knows a PR number and has never heard
    of a plan. While this matched `item` alone, the rows the fleet actually produced
    attached to nothing and `next` went on handing the work out.
    """
    repo = f"acme/forge-{kind}"
    parked = await add_ref_item(client, "parked on a premise", repo, kind, "1697")
    free = await add_ref_item(client, "actually free", repo, kind, "1698")

    before = await plan(client, repo)
    assert before["next"]["item_id"] == parked, "precondition: it was next"

    r = await client.post("/blockers", json={
        "subject_kind": kind, "subject_value": "1697", "kind": "decision",
        "question": "does the premise hold?", "repo": repo}, headers=LAPTOP)
    assert r.status_code == 200, r.text

    after = await plan(client, repo)
    assert after["next"]["item_id"] == free, "next handed out work behind an open question"
    row = next(i for i in after["items"] if i["item_id"] == parked)
    (w,) = row["waiting_on_a_human"]
    assert w["question"] == "does the premise hold?"
    assert after["counts"]["waiting_on_a_human"] == 1


async def test_a_blocker_on_one_repos_42_does_not_park_anothers(client):
    """The scope is half of what a bare number means. `ix_plan_items_open_ref` is
    unique on `(COALESCE(repo, ''), ref_kind, ref_value)` and this comparison is that
    index's key — an item and a blocker that disagree about the repo are about two
    different `#42`s, which is `app.claimkey`'s rule already."""
    mine = await add_ref_item(client, "mine", "acme/scoped-a", "issue", "42")
    theirs = await add_ref_item(client, "theirs", "acme/scoped-b", "issue", "42")

    r = await client.post("/blockers", json={
        "subject_kind": "issue", "subject_value": "42", "kind": "decision",
        "question": "which?", "repo": "acme/scoped-a"}, headers=LAPTOP)
    assert r.status_code == 200, r.text

    blocked = await plan(client, "acme/scoped-a")
    assert next(i for i in blocked["items"]
                if i["item_id"] == mine)["waiting_on_a_human"]
    untouched = await plan(client, "acme/scoped-b")
    assert not next(i for i in untouched["items"]
                    if i["item_id"] == theirs)["waiting_on_a_human"]
    assert untouched["next"]["item_id"] == theirs


async def test_a_blocker_naming_a_forge_ref_nobody_planned_attaches_to_nothing(client):
    """Unchanged and correct: it is a real question in the queue, and there is no
    plan row for it to hold up. The row is not lost — it is simply not an edge."""
    repo = "acme/unplanned"
    only = await add_ref_item(client, "planned", repo, "issue", "1")
    r = await client.post("/blockers", json={
        "subject_kind": "issue", "subject_value": "999", "kind": "decision",
        "question": "about nothing on the plan", "repo": repo}, headers=LAPTOP)
    assert r.status_code == 200, r.text

    after = await plan(client, repo)
    assert after["next"]["item_id"] == only
    assert after["counts"]["waiting_on_a_human"] == 0
    listed = await client.get("/blockers", params={"repo": repo}, headers=LAPTOP)
    assert any(b["question"] == "about nothing on the plan"
               for b in listed.json()["blockers"]), "still queued, just not an edge"


async def test_an_answered_forge_blocker_releases_the_item(client):
    """The edge is the OPEN question. Answering it is what makes the work next
    again, and nothing has to remember to undo anything."""
    repo = "acme/forge-answered"
    parked = await add_ref_item(client, "parked", repo, "pr", "7")
    r = await client.post("/blockers", json={
        "subject_kind": "pr", "subject_value": "7", "kind": "decision",
        "question": "does the premise hold?", "repo": repo}, headers=LAPTOP)
    blocker_id = r.json()["blocker"]["id"]
    assert (await plan(client, repo))["next"] is None

    done = await client.post("/blockers/resolve",
                             json={"blocker_id": blocker_id,
                                   "resolution": "it does not — revert the flag"},
                             headers=HUMAN)
    assert done.status_code == 200, done.text
    assert (await plan(client, repo))["next"]["item_id"] == parked


async def test_a_fleet_scope_blocker_reaches_a_fleet_scope_item(client):
    """Both `repo` columns are nullable and NULL means FLEET SCOPE — a real value,
    not a missing one. NULL never equals NULL in SQL, so an ordinary comparison
    drops this pair silently; the COALESCE on both sides is what keeps it, and it
    is `ix_plan_items_open_ref`'s own spelling. Untested, this is a path that reads
    as working and parks nothing."""
    r = await client.post("/plan/item", json={"title": "fleet-wide chore",
                                              "ref_kind": "issue", "ref_value": "8801"},
                          headers=LAPTOP)
    assert r.status_code in (200, 201), r.text
    item = r.json()["item_id"]
    MINE.add(item)

    raised = await client.post("/blockers", json={
        "subject_kind": "issue", "subject_value": "8801", "kind": "decision",
        "question": "fleet-wide: which?"}, headers=LAPTOP)
    assert raised.status_code == 200, raised.text

    plan_all = await client.get("/plan", headers=LAPTOP)
    row = next(i for i in plan_all.json()["items"] if i["item_id"] == item)
    assert row["waiting_on_a_human"], "a NULL-repo blocker must reach a NULL-repo item"


async def test_a_scoped_blocker_does_not_reach_a_fleet_scope_item_of_the_same_number(
        client):
    """The other half of the same rule, and the direction that would park work
    wrongly: `#8802` in one repo is not the fleet-wide `#8802`."""
    r = await client.post("/plan/item", json={"title": "fleet-wide, unblocked",
                                              "ref_kind": "issue", "ref_value": "8802"},
                          headers=LAPTOP)
    item = r.json()["item_id"]
    MINE.add(item)
    await client.post("/blockers", json={
        "subject_kind": "issue", "subject_value": "8802", "kind": "decision",
        "question": "about one repo's 8802", "repo": "acme/not-fleet"}, headers=LAPTOP)

    plan_all = await client.get("/plan", headers=LAPTOP)
    row = next(i for i in plan_all.json()["items"] if i["item_id"] == item)
    assert not row["waiting_on_a_human"], "a repo's question must not park fleet work"


async def test_an_item_carries_both_an_item_blocker_and_a_forge_one(client):
    """The two paths are additive, not alternatives — an item named directly and by
    its ref collects both questions, each answerable on its own."""
    repo = "acme/both-paths"
    item = await add_ref_item(client, "asked about twice", repo, "pr", "31")
    await client.post("/blockers", json={
        "subject_kind": "item", "subject_value": item, "kind": "taste",
        "question": "the right shape?", "repo": repo}, headers=LAPTOP)
    await client.post("/blockers", json={
        "subject_kind": "pr", "subject_value": "31", "kind": "decision",
        "question": "does the premise hold?", "repo": repo}, headers=LAPTOP)

    after = await plan(client, repo)
    row = next(i for i in after["items"] if i["item_id"] == item)
    assert {w["class"] for w in row["waiting_on_a_human"]} == {"taste", "decision"}
    assert after["counts"]["waiting_on_a_human"] == 1, "one item, however many questions"
