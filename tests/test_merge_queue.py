"""v2.61: the line to land on a branch — #227, FIFO first cut.

`kind='merge'` says *somebody is landing right now* and cannot say who is next.
So every review-clean PR behaved as though it were: merge the base, push, wait
for CI, re-run preland, find somebody else landed, repeat — #80's quadratic
integration cost, and each loser's push invalidating the winners' green checks on
the way past.

The properties under test are the ones that distinguish a queue from a second
lock:

* **Only the head may move, and a non-head is told exactly why.** A refusal that
  says "not yet" leaves an agent nothing to do but poll; one that says "queued
  behind #123, position 3" tells it whom to talk to and what to wait for.
* **A non-head is stopped BEFORE it spends anything.** No rebase, no push, no CI
  run — the whole cost this exists to remove is paid in the first ten seconds of
  a land, not at the end.
* **The queue advances on its own** when the head lands, stands down, or stops
  answering. A wedged head would block everybody's landing.
* **Readiness is about a commit, not a memory.** A head change invalidates it.
* **Enqueue is idempotent and never costs a place.** An agent that has just been
  refused is meant to call it again on its next poll.
* **It is not a second lock.** No path here writes a `kind='merge'` claim, and
  the head being ready confers no claim at all — it says go and ask for one.

Deliberately absent, and asserted as absent: ordering proposals. #227's own
argument is that agents proposing an order while trying to land makes the queue
"another shared resource every agent thrashes", so the first cut is strict FIFO
and `suggested_order` is permanently null.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.db import async_session
from app.models.merge_queue import MergeQueueEntry

from .conftest import DESKTOP, LAPTOP, SERVER

REPO = "acme/queuerepo"
BASE = "main"

#: Full oids, because the endpoint refuses anything shorter — the head is the
#: whole invalidation mechanism, and a value that cannot be compared is a
#: readiness that never expires.
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


async def enqueue(client, pr: int, head: str = SHA_A, headers=LAPTOP, *,
                  repo: str = REPO, base: str = BASE, **over):
    body = {"repo": repo, "base": base, "pr": pr, "head": head, **over}
    return await client.post("/merge-queue/enqueue", json=body, headers=headers)


async def join(client, pr: int, head: str = SHA_A, headers=LAPTOP, **over) -> dict:
    r = await enqueue(client, pr, head, headers, **over)
    assert r.status_code == 200, r.text
    return r.json()


async def read(client, *, repo: str = REPO, base: str = BASE, headers=LAPTOP,
               **params) -> dict:
    r = await client.get("/merge-queue",
                         params={"repo": repo, "base": base, **params},
                         headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def leave(client, pr: int, reason: str = "merged", headers=LAPTOP, *,
                repo: str = REPO, base: str = BASE) -> dict:
    r = await client.post("/merge-queue/leave",
                          json={"repo": repo, "base": base, "pr": pr, "reason": reason},
                          headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _expire(repo: str, base: str, pr: int) -> None:
    """Age an entry out, instead of waiting for the wall clock to do it."""
    async with async_session() as s:
        await s.execute(
            update(MergeQueueEntry)
            .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
                   MergeQueueEntry.pr == pr, MergeQueueEntry.left_at.is_(None))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()


# ------------------------------------------------ only the head may proceed


async def test_only_the_head_may_merge_and_the_second_may_not_even_integrate(client):
    """The finding this table exists for.

    Two ready PRs on one base. Both were review-clean, both were preland READY,
    and before the queue both would have integrated, pushed and spent a CI run to
    discover that only one of them could land. The second's permissions are the
    assertion: `may_integrate` is false as well as `may_merge`, because the
    expensive half is the integration, not the merge.
    """
    repo = "acme/onlyhead"
    first = await join(client, 101, SHA_A, repo=repo, verdict="ready")
    second = await join(client, 102, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")

    assert first["you"]["is_head"] is True
    assert first["you"]["position"] == 1
    assert first["you"]["may_integrate"] is True
    assert first["you"]["may_merge"] is True

    assert second["you"]["is_head"] is False
    assert second["you"]["position"] == 2
    assert second["you"]["may_merge"] is False
    assert second["you"]["may_integrate"] is False


async def test_a_non_head_is_told_which_pr_it_waits_behind_and_at_what_position(client):
    """"Not yet" leaves an agent nothing to do but poll. The reason has to name
    the PR ahead, the position, and the thing it must not do — a refusal an agent
    can act on is the entire coordination value here."""
    repo = "acme/behind"
    await join(client, 201, SHA_A, repo=repo, verdict="ready", note="landing the migration")
    await join(client, 202, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")
    third = await join(client, 203, SHA_C, repo=repo, headers=SERVER, verdict="ready")

    you = third["you"]
    assert you["position"] == 3
    assert "queued behind #201, position 3" in you["reason"]
    # And what NOT to do, said out loud: the cost being removed is the CI run, so
    # a refusal that omits it invites the agent to spend it anyway.
    assert "push" in you["reason"] and "CI" in you["reason"]
    assert you["waiting_on"]["pr"] == 201
    assert you["waiting_on"]["note"] == "landing the migration"


async def test_the_queue_reports_one_order_and_one_head_to_every_reader(client):
    repo = "acme/oneorder"
    await join(client, 301, SHA_A, repo=repo, verdict="ready")
    await join(client, 302, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")

    view = await read(client, repo=repo)
    assert view["active_order"] == [301, 302]
    assert view["head"]["pr"] == 301
    assert view["counts"] == {"queued": 2, "ready": 2, "not_ready": 0}
    # Every reader, not just the queued ones: a peer with a different token sees
    # the same line, which is what makes it a board rather than a local note.
    assert (await read(client, repo=repo, headers=SERVER))["active_order"] == [301, 302]


# ----------------------------------------------------- the queue advances


async def test_the_queue_advances_when_the_head_leaves(client):
    """The head merged, so #402 becomes head — without anyone hand-editing a
    local note, which is how this was tracked before."""
    repo = "acme/advance"
    await join(client, 401, SHA_A, repo=repo, verdict="ready")
    await join(client, 402, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")
    assert (await read(client, repo=repo, pr=402))["you"]["may_merge"] is False

    out = await leave(client, 401, "merged", repo=repo)
    assert out["left"] is True
    assert out["active_order"] == [402]

    now_head = await read(client, repo=repo, pr=402)
    assert now_head["you"]["position"] == 1
    assert now_head["you"]["is_head"] is True
    assert now_head["you"]["may_merge"] is True


async def test_a_head_that_stops_answering_is_swept_and_the_queue_moves_on(client):
    """Passive expiry, borrowed from the claim table. There is no reaper — a
    wedged head would block everybody's landing, so the way out cannot depend on
    the wedged agent doing anything."""
    repo = "acme/lapsehead"
    await join(client, 501, SHA_A, repo=repo, verdict="ready")
    await join(client, 502, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")
    await _expire(repo, BASE, 501)

    # The read filters it out without mutating: a read must not sweep.
    assert (await read(client, repo=repo))["active_order"] == [502]
    # ...and the next write actually retires it, marked as lapsed rather than as
    # a clean stand-down: "landed" and "stopped answering" are different facts.
    await join(client, 502, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")
    async with async_session() as s:
        row = await s.scalar(
            update(MergeQueueEntry)
            .where(MergeQueueEntry.repo == repo, MergeQueueEntry.pr == 501)
            .values(note=None).returning(MergeQueueEntry.lapsed))
        await s.commit()
    assert row is True


async def test_leaving_is_idempotent_and_records_who_and_why(client):
    repo = "acme/leavetwice"
    await join(client, 601, SHA_A, repo=repo, verdict="ready")
    first = await leave(client, 601, "merged as 6b2f", repo=repo)
    assert first["left"] is True
    # Not a 404 the second time: "this PR is not in the queue" is the state the
    # caller wanted, and an agent tidying up after a merge should not have to care
    # whether the TTL beat it to it.
    second = await leave(client, 601, "merged as 6b2f", repo=repo)
    assert second["left"] is False
    assert second["active_order"] == []

    async with async_session() as s:
        entry = await s.scalar(
            update(MergeQueueEntry)
            .where(MergeQueueEntry.repo == repo, MergeQueueEntry.pr == 601)
            .values(ttl_seconds=MergeQueueEntry.ttl_seconds)
            .returning(MergeQueueEntry.left_reason))
        await s.commit()
    assert entry == "merged as 6b2f"


async def test_a_peer_may_retire_an_abandoned_entry_and_the_record_says_who_did(client):
    """The agent best placed to notice a dead head is the one behind it. If only
    the owner could stand an entry down, the TTL would be the only way out of
    exactly the case the TTL is the crude fallback for — so anyone may, and the
    row records who."""
    repo = "acme/peerleave"
    await join(client, 701, SHA_A, repo=repo, verdict="ready")
    await join(client, 702, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")

    out = await leave(client, 701, "closed without merging", headers=DESKTOP, repo=repo)
    assert out["left"] is True and out["active_order"] == [702]
    async with async_session() as s:
        who = await s.scalar(
            update(MergeQueueEntry)
            .where(MergeQueueEntry.repo == repo, MergeQueueEntry.pr == 701)
            .values(ttl_seconds=MergeQueueEntry.ttl_seconds)
            .returning(MergeQueueEntry.left_by))
        await s.commit()
    assert who == "desktop"


# ------------------------------------------- readiness is about a commit


async def test_a_head_change_invalidates_readiness_without_costing_the_slot(client):
    """An agent remembers "preland said READY". It does not reliably notice that
    the thing preland said it about was three pushes ago.

    Reporting the new head is enough: the reader passes the commit it can see, and
    the board says the entry is behind the branch. The slot is untouched — pushing
    is exactly what a head's slot is for, and demoting it would hand the line to a
    PR that then invalidates the first one's checks."""
    repo = "acme/headmoved"
    await join(client, 801, SHA_A, repo=repo, verdict="ready")
    assert (await read(client, repo=repo, pr=801))["you"]["may_merge"] is True

    moved = await read(client, repo=repo, pr=801, head=SHA_D)
    assert moved["you"]["may_merge"] is False
    assert moved["you"]["position"] == 1 and moved["you"]["is_head"] is True
    assert "re-run preland" in moved["you"]["reason"]
    # Integrating stays allowed: it is how a head gets back to ready.
    assert moved["you"]["may_integrate"] is True


async def test_re_enqueueing_at_a_new_head_without_preland_clears_the_ready_verdict(client):
    """The other half of the same rule. An agent that pushes and re-registers
    honestly — reporting the new head with no fresh preland — must not carry the
    old READY forward onto a commit nobody checked."""
    repo = "acme/pushed"
    await join(client, 901, SHA_A, repo=repo, verdict="ready")
    pushed = await join(client, 901, SHA_B, repo=repo, verdict="queued")

    assert pushed["entry"]["head"] == SHA_B
    assert pushed["entry"]["ready_sha"] is None
    assert pushed["entry"]["ready"] is False
    assert pushed["you"]["may_merge"] is False
    assert pushed["you"]["may_integrate"] is True

    # And re-running preland at the new head restores it.
    rerun = await join(client, 901, SHA_B, repo=repo, verdict="ready")
    assert rerun["entry"]["ready_sha"] == SHA_B
    assert rerun["you"]["may_merge"] is True


async def test_the_database_refuses_a_ready_verdict_about_another_commit(client):
    """The single guarantee this table adds over an agent's own memory, enforced
    where every write path gets it rather than where each one must remember.

    An agent remembers "preland said READY" and does not reliably notice that the
    thing preland said it about was three pushes ago. A row that carried `ready`
    while sitting on a different commit would be that memory, written down and
    made authoritative — so `ck_merge_queue_ready_at_head` refuses it outright.
    """
    async with async_session() as s:
        s.add(MergeQueueEntry(
            repo="acme/constraint", base=BASE, pr=1, head_sha=SHA_A,
            # The lie: ready, but ready about a commit this entry is not on.
            ready_sha=SHA_B, verdict="ready", holder="laptop",
            ttl_seconds=60, expires_at=datetime.now(UTC) + timedelta(seconds=60)))
        with pytest.raises(IntegrityError) as caught:
            await s.commit()
        await s.rollback()
    assert "ck_merge_queue_ready_at_head" in str(caught.value)


async def test_a_head_with_a_non_ready_verdict_may_integrate_but_not_merge(client):
    """`reconcile` is preland's "your base is stale" — a block that landing in
    turn dissolves, so it belongs in the line. It is not permission to merge."""
    repo = "acme/reconcile"
    out = await join(client, 1001, SHA_A, repo=repo, verdict="reconcile")
    assert out["you"]["is_head"] is True
    assert out["you"]["may_integrate"] is True
    assert out["you"]["may_merge"] is False
    assert "reconcile" in out["you"]["reason"]


async def test_a_pr_that_is_genuinely_blocked_is_refused_entry(client):
    """preland HOLD means something is wrong with the PR, not with its turn. An
    entry at the head that can never land holds the line until its TTL runs
    out, so the gate is on the way in."""
    r = await enqueue(client, 1101, SHA_A, repo="acme/held", verdict="hold")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["verdicts"] == ["ready", "reconcile", "queued"]
    assert "HOLD" in detail["hint"]
    # And nothing was written: a refused PR is not quietly in the line.
    assert (await read(client, repo="acme/held"))["active_order"] == []


async def test_a_head_that_is_not_a_commit_id_is_refused_rather_than_dropped(client):
    """The opposite trade from `review_runs.head_sha`, which drops a garbled head
    rather than lose a run's findings. Here the head is the whole invalidation
    mechanism, so a value that cannot be compared would be a permanent green
    light rather than a missing field."""
    r = await enqueue(client, 1201, "z" * 40, repo="acme/badsha", verdict="ready")
    assert r.status_code == 422, r.text
    assert "not a commit id" in r.json()["detail"]["error"]
    # An abbreviation is refused too, and for the same reason: it cannot be
    # compared against the oid `gh` reports without the repo that minted it.
    short = await enqueue(client, 1202, SHA_A[:12], repo="acme/badsha", verdict="ready")
    assert short.status_code == 422, short.text


# ------------------------------------------------------- enqueue is idempotent


async def test_re_enqueueing_never_costs_a_place_in_the_line(client):
    """An agent that has just been refused is *meant* to call this again on its
    next poll. A queue that sent it to the back for doing so would be a queue
    nobody could safely poll — the same "spend work to learn you are waiting"
    this exists to remove."""
    repo = "acme/idempotent"
    first = await join(client, 1301, SHA_A, repo=repo, verdict="ready")
    await join(client, 1302, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")

    again = await join(client, 1301, SHA_A, repo=repo, verdict="ready",
                       note="still landing")
    assert again["entry"]["entry_id"] == first["entry"]["entry_id"]
    assert again["entry"]["position"] == 1
    assert again["entry"]["entered"] == first["entry"]["entered"]
    assert again["entry"]["note"] == "still landing"
    assert again["active_order"] == [1301, 1302]

    # Three more polls, still one row and still one place.
    for _ in range(3):
        await join(client, 1301, SHA_A, repo=repo, verdict="ready")
    assert (await read(client, repo=repo))["active_order"] == [1301, 1302]


async def test_a_pr_that_left_and_came_back_goes_to_the_back(client):
    """The other side of idempotency, and it must not be idempotent: standing
    down and re-entering is a new arrival, or leaving would be a free way to keep
    a slot while doing something else."""
    repo = "acme/rejoin"
    await join(client, 1401, SHA_A, repo=repo, verdict="ready")
    await join(client, 1402, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")
    await leave(client, 1401, "superseded", repo=repo)
    await join(client, 1401, SHA_A, repo=repo, verdict="ready")
    assert (await read(client, repo=repo))["active_order"] == [1402, 1401]


# ------------------------------------- ordering around the claim, not a lock


async def test_being_at_the_head_takes_no_merge_claim(client):
    """The line this feature must not cross. #99's claim is the one place that
    says a land is in progress; a queue that also held it would be the second
    implementation of "who has this right now" that #99 was filed to avoid."""
    repo = "acme/noclaim"
    out = await join(client, 1501, SHA_A, repo=repo, verdict="ready")
    assert out["you"]["may_merge"] is True
    assert out["claim"]["held"] is False

    # Nothing anywhere on the claims table, under any key.
    claims = await client.get("/claims", params={"limit": 1000}, headers=LAPTOP)
    keys = [c["key"] for c in claims.json()["claims"]]
    assert f"{repo}:{BASE}" not in keys

    # And the claim is still there to be taken, by the head, in the ordinary way.
    took = await client.post("/claim", json={"kind": "merge", "key": f"{repo}:{BASE}",
                                             "note": "landing #1501"}, headers=LAPTOP)
    assert took.status_code == 200, took.text
    assert took.json()["claimed"] is True


async def test_the_head_is_told_who_holds_the_claim_rather_than_overriding_them(client):
    """A human merging in the UI, or an agent that never enqueued, lands
    regardless. The queue reports the holder; it does not outrank them, and it
    does not refuse them either."""
    repo = "acme/claimheld"
    outsider = await client.post(
        "/claim", json={"kind": "merge", "key": f"{repo}:{BASE}",
                        "note": "landing by hand"}, headers=SERVER)
    assert outsider.status_code == 200, outsider.text

    out = await join(client, 1601, SHA_A, repo=repo, verdict="ready")
    assert out["you"]["is_head"] is True
    assert out["claim"]["held"] is True
    assert out["claim"]["holder"] == "server"
    assert out["claim"]["note"] == "landing by hand"
    # Reported, not enforced: the queue never turns a claim into a refusal, and
    # never quietly renews or steals one.
    assert out["you"]["may_merge"] is True
    assert "take" in out["you"]["reason"] and "kind=merge" in out["you"]["reason"]


async def test_leaving_the_queue_does_not_release_the_merge_claim(client):
    """Two resources, two lifecycles. A queue that released claims on its own
    would be able to free a land another agent was mid-way through."""
    repo = "acme/leaveclaim"
    await join(client, 1701, SHA_A, repo=repo, verdict="ready")
    took = await client.post("/claim", json={"kind": "merge", "key": f"{repo}:{BASE}"},
                             headers=LAPTOP)
    assert took.status_code == 200

    out = await leave(client, 1701, "merged", repo=repo)
    assert out["claim"]["held"] is True
    assert out["claim"]["holder"] == "laptop"


# -------------------------------------------------- FIFO, and only FIFO


async def test_the_first_cut_offers_no_suggested_order_and_says_so(client):
    """#227 asks for `active_order` and `suggested_order` to be distinguishable
    so a proposal can never look like the live order. The distinction ships from
    the first cut with nothing able to populate the second half — agents
    proposing an order while trying to land is the thrash the issue warns about,
    and it needs the acceptance machinery this release does not have."""
    repo = "acme/fifo"
    await join(client, 1801, SHA_A, repo=repo, verdict="ready")
    view = await read(client, repo=repo)
    assert view["ordering"] == "fifo"
    assert view["active_order"] == [1801]
    assert view["suggested_order"] is None

    for path in ("/merge-queue/order-proposal", "/merge-queue/reorder"):
        r = await client.post(path, json={}, headers=LAPTOP)
        assert r.status_code == 404, f"{path} is #227's second half, not this one"


async def test_two_bases_in_one_repo_are_two_queues(client):
    """Keyed on `repo + base`, exactly as the `kind='merge'` claim is. Landing on
    a release branch is not waiting for anything landing on main."""
    repo = "acme/twobases"
    await join(client, 1901, SHA_A, repo=repo, base="main", verdict="ready")
    await join(client, 1902, SHA_B, repo=repo, base="release/2.x",
               headers=DESKTOP, verdict="ready")

    assert (await read(client, repo=repo, base="main", pr=1901))["you"]["may_merge"] is True
    other = await read(client, repo=repo, base="release/2.x", pr=1902)
    assert other["you"]["may_merge"] is True
    assert other["active_order"] == [1902]


async def test_a_repo_or_base_the_board_cannot_key_is_refused(client):
    """The queue derives its scope through `app.claimkey`, so it cannot open a
    line on a ref that cannot exist — a coordination key nobody can contend over
    is #172 in miniature."""
    bare = await enqueue(client, 2001, SHA_A, repo="queuerepo", verdict="ready")
    assert bare.status_code == 422
    ranged = await enqueue(client, 2002, SHA_A, repo=REPO, base="feat~1", verdict="ready")
    assert ranged.status_code == 422


async def test_the_repo_is_case_folded_so_one_repo_is_one_queue(client):
    """`Acme/Widget` and `acme/widget` are one repository on GitHub, and two
    queues here would be #148 reproduced in a new table."""
    await join(client, 2101, SHA_A, repo="Acme/CaseQueue", verdict="ready")
    view = await read(client, repo="acme/casequeue")
    assert view["repo"] == "acme/casequeue"
    assert view["active_order"] == [2101]


async def test_a_pr_that_never_enqueued_is_told_to_enqueue_not_told_to_go(client):
    """Silence must not read as permission. An agent asking about a PR nobody
    put in the line gets a no with the remedy, not an unconditioned yes."""
    repo = "acme/notqueued"
    await join(client, 2201, SHA_A, repo=repo, verdict="ready")
    you = (await read(client, repo=repo, pr=2299))["you"]
    assert you["queued"] is False
    assert you["may_merge"] is False and you["may_integrate"] is False
    assert "enqueue" in you["reason"]


async def test_a_head_without_a_pr_is_refused_rather_than_ignored(client):
    """A caller that believed it was asking "is my entry stale" and silently got
    the unconditioned answer is the failure the parameter exists to prevent."""
    r = await client.get("/merge-queue",
                         params={"repo": REPO, "base": BASE, "head": SHA_A},
                         headers=LAPTOP)
    assert r.status_code == 422, r.text


async def test_the_queue_is_not_readable_without_a_token(client):
    r = await client.get("/merge-queue", params={"repo": REPO, "base": BASE})
    assert r.status_code == 401
    w = await client.post("/merge-queue/enqueue",
                          json={"repo": REPO, "base": BASE, "pr": 1, "head": SHA_A})
    assert w.status_code == 401


# ------------------------------- a leave landing in the gap (codex, round 1)


def _losing_update(mq, on_calls: set[int]):
    """`mq.update`, but the named calls match nothing — a leave landing in the gap.

    `_join` SELECTs the live entry and then UPDATEs it conditional on
    `left_at IS NULL`. A concurrent `leave` committing between those two makes
    the UPDATE match zero rows, and that interleaving cannot be produced from a
    test that shares one event loop with the request. Adding `AND false` to the
    statement produces the same zero rows at the same line, which is the thing
    under test: what the endpoint does when its conditional write loses.

    Call 1 in an enqueue is `_sweep_lapsed`'s; `_join`'s are 2 onwards.
    """
    real, calls = mq.update, {"n": 0}

    def racing(*a, **kw):
        calls["n"] += 1
        stmt = real(*a, **kw)
        return stmt.where(sa_text("false")) if calls["n"] in on_calls else stmt

    return real, racing


async def test_an_enqueue_whose_conditional_write_loses_recovers_instead_of_crashing(client):
    """A peer's `leave` lands between the enqueue's SELECT and its write.

    Read-then-write would have stamped a fresh expiry onto a row that had already
    left: resurrecting nothing, reporting success, and then failing to find its
    own entry in the live queue — a 500 out of an idempotent endpoint. The write
    is conditional on `left_at IS NULL` instead, so this pass simply loses and
    the next one re-reads and gets it right.
    """
    import app.api.merge_queue as mq

    repo = "acme/raceleave"
    first = await join(client, 2301, SHA_A, repo=repo, verdict="ready")
    await join(client, 2302, SHA_B, repo=repo, headers=DESKTOP, verdict="ready")

    real, racing = _losing_update(mq, {2})
    mq.update = racing
    try:
        again = await join(client, 2301, SHA_B, repo=repo, verdict="ready")
    finally:
        mq.update = real

    # Recovered on the second pass: same row, same place, and the update it lost
    # the first time actually applied.
    assert again["entry"]["entry_id"] == first["entry"]["entry_id"]
    assert again["entry"]["head"] == SHA_B
    assert again["you"]["position"] == 1
    assert again["active_order"] == [2301, 2302]


async def test_an_enqueue_that_cannot_settle_is_a_409_and_never_a_500(client):
    """Both passes losing is not supposed to be reachable — one PR is driven by
    one agent — so what matters is that the unreachable case is an answer rather
    than a traceback. An agent told "contended, try again" retries; one handed a
    500 has learned nothing about a queue it is still sitting in."""
    import app.api.merge_queue as mq

    repo = "acme/nosettle"
    await join(client, 2501, SHA_A, repo=repo, verdict="ready")

    real, racing = _losing_update(mq, {2, 3})
    mq.update = racing
    try:
        r = await enqueue(client, 2501, SHA_B, repo=repo, verdict="ready")
    finally:
        mq.update = real

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["pr"] == 2501
    # And the entry is untouched — a refusal must not half-apply.
    assert (await read(client, repo=repo))["head"]["head"] == SHA_A


async def test_an_entry_retired_between_the_write_and_the_read_is_reported_not_crashed(client):
    """The response renders the entry's position out of the live queue, and the
    entry can be gone by then — a peer's `leave` commits in the gap. Looking the
    position up with `.index()` raised there, turning a successful enqueue into a
    500. Position is `null` instead, and `you` says the PR is not in the line."""
    import app.api.merge_queue as mq

    repo = "acme/vanished"
    real_live = mq._live_entries

    async def empty_queue(*a, **kw):
        return []

    mq._live_entries = empty_queue
    try:
        out = await join(client, 2401, SHA_A, repo=repo, verdict="ready")
    finally:
        mq._live_entries = real_live

    assert out["entry"]["position"] is None
    assert out["active_order"] == []
    assert out["you"]["queued"] is False
    assert out["you"]["may_merge"] is False


# ------------------------------------ what a second codex pass found (round 2)


async def test_a_ready_verdict_with_no_commit_pinned_to_it_is_refused(client):
    """The shape `ck_merge_queue_ready_at_head` exists to refuse was the one it
    let through.

    Written `ready_sha = head_sha`, a row with `verdict='ready'` and a NULL
    `ready_sha` evaluates FALSE OR NULL, which is NULL — and a CHECK passes on
    anything that is not FALSE. So a ready verdict pinned to no commit at all
    was storable: a readiness that can never be shown to have expired, which is
    strictly worse than one pinned to the wrong commit.
    """
    async with async_session() as s:
        s.add(MergeQueueEntry(
            repo="acme/nullready", base=BASE, pr=1, head_sha=SHA_A,
            ready_sha=None, verdict="ready", holder="laptop",
            ttl_seconds=60, expires_at=datetime.now(UTC) + timedelta(seconds=60)))
        with pytest.raises(IntegrityError) as caught:
            await s.commit()
        await s.rollback()
    assert "ck_merge_queue_ready_at_head" in str(caught.value)


async def test_an_overtaken_enqueue_does_not_put_a_stale_ready_verdict_back(client):
    """Two enqueues for one PR can be in flight at once — an agent that pushed
    and re-registered while its previous poll was still on the wire.

    Last-writer-wins would let the older one land second and restore `ready` at a
    commit the PR has moved off, which is exactly the stale green light this
    feature exists to remove. The write is guarded on `updated_at <= now`, so the
    older request loses at the database and is handed the newer state instead of
    overwriting it."""
    import app.api.merge_queue as mq

    repo = "acme/overtaken"
    # The newer request: the PR has pushed and is honestly no longer ready.
    await join(client, 2601, SHA_A, repo=repo, verdict="ready")
    await join(client, 2601, SHA_B, repo=repo, verdict="queued")

    real_now = mq._utcnow
    # The older one, arriving late: it still believes SHA_A and still says ready.
    mq._utcnow = lambda: real_now() - timedelta(minutes=5)
    try:
        late = await join(client, 2601, SHA_A, repo=repo, verdict="ready")
    finally:
        mq._utcnow = real_now

    assert late["entry"]["head"] == SHA_B
    assert late["entry"]["verdict"] == "queued"
    assert late["entry"]["ready_sha"] is None
    assert late["you"]["may_merge"] is False
    # And the row really is untouched, not merely reported that way.
    assert (await read(client, repo=repo))["head"]["head"] == SHA_B


async def test_an_obsolete_enqueue_cannot_resurrect_a_pr_that_has_left(client):
    """An enqueue in flight when somebody stood the entry down.

    Its retry pass finds no live row and would insert one — putting a PR that has
    merged back at the end of a line it has no business being in. It expires on
    its own, but a stale record of a claim nobody is making is worse than none:
    it is a second answer to a question that already has one. A leave that landed
    AFTER the request started is refused; one that landed before it is the
    ordinary re-join and still goes to the back."""
    import app.api.merge_queue as mq

    repo = "acme/resurrect"
    await join(client, 2701, SHA_A, repo=repo, verdict="ready")
    await leave(client, 2701, "merged", repo=repo)

    real_now = mq._utcnow
    mq._utcnow = lambda: real_now() - timedelta(minutes=5)
    try:
        r = await enqueue(client, 2701, SHA_A, repo=repo, verdict="ready")
    finally:
        mq._utcnow = real_now

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["left_reason"] == "merged"
    assert (await read(client, repo=repo))["active_order"] == []

    # ...and an honest re-join, whose request starts after the leave, is fine.
    back = await join(client, 2701, SHA_A, repo=repo, verdict="ready")
    assert back["you"]["position"] == 1


async def test_a_delayed_leave_cannot_retire_the_prs_next_place_in_the_line(client):
    """A retried or slow `leave` names an incarnation that has already gone. If
    it matched on `(repo, base, pr)` alone it would retire the PR's *next* entry
    — a PR that left, was reworked and re-enqueued would be silently dropped out
    of the queue by the tidy-up for its predecessor, which is the one failure a
    queue must never have."""
    import app.api.merge_queue as mq

    repo = "acme/staleleave"
    await join(client, 2801, SHA_A, repo=repo, verdict="ready")
    await leave(client, 2801, "closed", repo=repo)
    await join(client, 2801, SHA_B, repo=repo, verdict="ready")

    real_now = mq._utcnow
    mq._utcnow = lambda: real_now() - timedelta(minutes=5)
    try:
        late = await leave(client, 2801, "closed", repo=repo)
    finally:
        mq._utcnow = real_now

    assert late["left"] is False
    # The new entry is untouched and still at the head.
    assert late["active_order"] == [2801]
    still = await read(client, repo=repo, pr=2801)
    assert still["head"]["head"] == SHA_B
    assert still["you"]["may_merge"] is True
