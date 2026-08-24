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
* **Asking where you are keeps your place, and only your own** (#405). Every act
  that used to renew an entry is an act the queue's own `reason` tells a waiter not
  to take, so the entries that lapsed were the well-behaved ones. A read naming a
  PR renews it when the caller holds it; a peer, a person and a monitor renew
  nothing, and a lapsed entry is not revived.
* **It is not a second lock.** No path here writes a `kind='merge'` claim, and
  the head being ready confers no claim at all — it says go and ask for one.

Deliberately absent, and asserted as absent: queue MUTATION. #227's own argument
is that agents rewriting an order while trying to land makes the queue "another
shared resource every agent thrashes", so `active_order` is strict FIFO and the
`order-proposal` / `reorder` endpoints that would put a proposal into force are
still #227's second half. #80 added an advisory `suggested_order` beside it —
tested in `test_merge_queue_ranking.py`, and asserted here only in the negative:
it never becomes the line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

import app.api.merge_queue as mq
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


async def _entry(pr: int, *, repo: str = REPO, base: str = BASE) -> MergeQueueEntry:
    """The live row for a PR, read behind the endpoint's back."""
    async with async_session() as s:
        got = await s.scalar(
            select(MergeQueueEntry).where(
                MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
                MergeQueueEntry.pr == pr, MergeQueueEntry.left_at.is_(None)))
        assert got is not None, f"#{pr} has no live entry"
        return got


async def _nearly_expired(pr: int, seconds: int = 30, *, repo: str = REPO,
                          base: str = BASE) -> datetime:
    """Wind an entry's expiry down to `seconds` away, and say where it now sits.

    Renewal is `now + ttl_seconds`, so an entry left at its full window moves by
    only the microseconds a request takes — a difference a test could assert on
    and learn nothing from. Winding it down first makes the renewal the size of
    the TTL, which is the thing that either happened or did not.
    """
    when = datetime.now(UTC) + timedelta(seconds=seconds)
    async with async_session() as s:
        await s.execute(
            update(MergeQueueEntry)
            .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
                   MergeQueueEntry.pr == pr, MergeQueueEntry.left_at.is_(None))
            .values(expires_at=when))
        await s.commit()
    return when


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


async def test_a_proposal_can_never_be_put_into_force(client):
    """#227 asks for `active_order` and `suggested_order` to be distinguishable so
    a proposal can never look like the live order. #80 populates the second half;
    what stays absent is anything that turns one into the other. Mutation needs a
    human or an accepted proposal, and neither endpoint exists — an agent that may
    silently rewrite the sequence is an agent with human privileges."""
    repo = "acme/fifo"
    await join(client, 1801, SHA_A, repo=repo, verdict="ready")
    view = await read(client, repo=repo)
    assert view["ordering"] == "fifo"
    assert view["active_order"] == [1801]
    # One entry: one arrangement, and no ranking is run for it.
    assert view["suggested_order"] is None
    assert view["suggestion"] is None

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


async def test_a_leave_naming_its_entry_cannot_retire_a_later_one(client):
    """A PR number names a pull request, not one of its stays in the queue.

    The timestamp guard separates two incarnations only while they overlap at
    the server; a leave delayed in transit arrives with a fresh timestamp and is
    indistinguishable from a prompt one. `entry_id` — which every enqueue
    returns — is the exact identification, and a leave that names a place the PR
    has already given up retires nothing rather than the place it holds now."""
    repo = "acme/entryid"
    first = await join(client, 2901, SHA_A, repo=repo, verdict="ready")
    old_id = first["entry"]["entry_id"]
    await leave(client, 2901, "closed", repo=repo)
    again = await join(client, 2901, SHA_B, repo=repo, verdict="ready")
    assert again["entry"]["entry_id"] != old_id

    stale = await client.post(
        "/merge-queue/leave",
        json={"repo": repo, "base": BASE, "pr": 2901, "reason": "closed",
              "entry_id": old_id}, headers=LAPTOP)
    assert stale.status_code == 200, stale.text
    assert stale.json()["left"] is False
    assert stale.json()["active_order"] == [2901]

    # ...and naming the CURRENT entry does stand it down.
    good = await client.post(
        "/merge-queue/leave",
        json={"repo": repo, "base": BASE, "pr": 2901, "reason": "merged",
              "entry_id": again["entry"]["entry_id"]}, headers=LAPTOP)
    assert good.status_code == 200 and good.json()["left"] is True
    assert good.json()["active_order"] == []


# ------------------------------------------------ #405: asking is what keeps your place


async def test_asking_where_you_are_in_the_line_is_what_keeps_your_place(client):
    """The fix, and the defect it is against.

    Every act that renewed an entry before this — enqueue at a new head, which in
    practice means a push — is an act the queue's own refusal tells a waiter not to
    take: *"do not rebase, push or restart CI"*. So the entries that lapsed were the
    ones whose agents obeyed, and the way to keep a place in the line was to ignore
    the advice the line gives you. A waiter asking where it is IS the liveness the
    TTL was approximating, so it is now measured directly.
    """
    repo = "acme/renew"
    await join(client, repo=repo, pr=601, headers=LAPTOP)
    close = await _nearly_expired(601, repo=repo)

    body = await read(client, repo=repo, pr=601, headers=LAPTOP)

    row = await _entry(601, repo=repo)
    assert row.expires_at > close + timedelta(minutes=25), (
        "a read by the entry's own holder must push the expiry out by the TTL")
    assert body["renewal"]["renewed"] is True
    assert "keeps your place" in body["renewal"]["why"]
    assert body["renewal"]["expires"] == row.expires_at.isoformat()
    # And the advice and the mechanism now agree, which is the whole repair.
    second = await read(client, repo=repo, pr=601, headers=LAPTOP)
    assert "asking is what keeps your place" not in second["you"]["reason"], (
        "the head is not the one being told to wait")


async def test_the_advice_that_used_to_cost_a_waiter_its_place_now_names_the_renewal(client):
    repo = "acme/advice"
    await join(client, repo=repo, pr=610, headers=LAPTOP)
    await join(client, repo=repo, pr=611, headers=SERVER)

    body = await read(client, repo=repo, pr=611, headers=SERVER)

    reason = body["you"]["reason"]
    assert "do not rebase, push or restart CI" in reason
    assert "asking is what keeps your place" in reason, (
        "a refusal that leaves an agent nothing to do that would renew its entry is "
        "the defect; the refusal has to name the thing that does")
    assert body["renewal"]["renewed"] is True


async def test_a_peer_reading_about_your_entry_renews_nothing(client):
    """The authorisation, and the property the TTL has left.

    A peer at position 2 watching the head is the most attentive reader the queue
    has — and its attention says nothing about whether the head's agent is alive. If
    that read renewed, a dead head would be held in place by the very agent it is
    blocking, which is exactly the case the timer exists for.
    """
    repo = "acme/peerread"
    await join(client, repo=repo, pr=602, headers=LAPTOP)
    close = await _nearly_expired(602, repo=repo)

    body = await read(client, repo=repo, pr=602, headers=DESKTOP)

    assert body["renewal"]["renewed"] is False
    assert "laptop" in body["renewal"]["why"]
    row = await _entry(602, repo=repo)
    assert abs((row.expires_at - close).total_seconds()) < 1


async def test_another_agent_on_the_holders_own_machine_renews_nothing(client):
    """A box runs several agents and they all authenticate as it.

    So "same machine" cannot be what authorises a renewal — it would let any agent
    on the box hold a dead peer's place. The refusal names the way out instead, and
    it is one that costs nothing: re-enqueueing rewrites the holder and leaves
    `entered_at` alone.
    """
    repo = "acme/sibling"
    driver = {**LAPTOP, "X-Agent-Key": "driver405"}
    sibling = {**LAPTOP, "X-Agent-Key": "sibling405"}
    entered = await join(client, repo=repo, pr=603, headers=driver)
    assert entered["entry"]["holder"].startswith("laptop/")
    close = await _nearly_expired(603, repo=repo)

    body = await read(client, repo=repo, pr=603, headers=sibling)

    assert body["renewal"]["renewed"] is False
    assert "this machine under another name" in body["renewal"]["why"]
    assert "enqueue #603 again" in body["renewal"]["why"]
    row = await _entry(603, repo=repo)
    assert abs((row.expires_at - close).total_seconds()) < 1


async def test_a_monitor_reading_the_whole_queue_renews_nothing(client):
    """`qb-doctor`, `qb-dash` and `qb-reconcile` read the line without naming a PR.

    That is why a bare read does not renew everything the caller holds: a poller on
    the same box would otherwise keep a dead agent's entry alive on a timer, and
    nothing on the board could tell that apart from an agent still working.
    """
    repo = "acme/monitor"
    await join(client, repo=repo, pr=604, headers=LAPTOP)
    close = await _nearly_expired(604, repo=repo)

    body = await read(client, repo=repo, headers=LAPTOP)

    assert "renewal" not in body, "a read that names no PR renews nothing and says so by silence"
    row = await _entry(604, repo=repo)
    assert abs((row.expires_at - close).total_seconds()) < 1


async def test_a_person_looking_at_the_board_renews_nobodys_entry(client):
    """The human board reads this endpoint too, through the edge.

    A person watching the queue is not evidence that the agent driving #605 is
    alive, and both tiers of browser identity are refused: an edge-vouched
    `human/rich`, who is somebody but is not the holder, and a bare `Remote-User`
    that resolves to nobody at all.
    """
    repo = "acme/browsing"
    await join(client, repo=repo, pr=605, headers=LAPTOP)
    close = await _nearly_expired(605, repo=repo)

    vouched = await read(client, repo=repo, pr=605,
                         headers={"Remote-User": "rich", "X-Edge-Auth": "tok-edge"})
    assert vouched["renewal"]["renewed"] is False
    assert "laptop" in vouched["renewal"]["why"]

    bare = await read(client, repo=repo, pr=605, headers={"Remote-User": "rich"})
    assert bare["renewal"]["renewed"] is False
    assert "no agent identity" in bare["renewal"]["why"]

    row = await _entry(605, repo=repo)
    assert abs((row.expires_at - close).total_seconds()) < 1


async def test_a_lapsed_entry_is_not_renewed_back_into_the_line(client):
    """Renewal holds a place; it does not restore one.

    An entry the queue has already stopped counting has let everybody behind it
    move, and quietly reviving it on the next poll would put a PR back in a line
    that has advanced past it. The holder is told there is no place to keep, and
    re-enqueues at the back — which is honest, and is what the response says.
    """
    repo = "acme/lapsed"
    await join(client, repo=repo, pr=606, headers=LAPTOP)
    await _expire(repo, BASE, 606)

    body = await read(client, repo=repo, pr=606, headers=LAPTOP)

    assert body["renewal"]["renewed"] is False
    assert "no live entry" in body["renewal"]["why"]
    assert body["active_order"] == []
    assert body["you"]["queued"] is False


async def test_a_renewal_moves_the_expiry_and_leaves_the_write_order_alone(client):
    """`updated_at` orders CONTENT writes, and a read must not compete in that order.

    `_join` refuses an enqueue stamped older than the row it would overwrite — that
    is what stops a slow poll putting a stale `ready` verdict back onto a commit the
    PR has moved off. A read that bumped `updated_at` would make an in-flight
    enqueue lose that comparison and be answered with the PR's old head, by the
    board, after it had just been told the new one.
    """
    repo = "acme/writeorder"
    await join(client, repo=repo, pr=607, headers=LAPTOP)
    before = await _entry(607, repo=repo)
    await _nearly_expired(607, repo=repo)

    await read(client, repo=repo, pr=607, headers=LAPTOP)

    after = await _entry(607, repo=repo)
    assert after.updated_at == before.updated_at
    assert after.head_sha == before.head_sha
    assert after.expires_at > before.expires_at


async def test_renewing_never_moves_a_pr_in_the_line(client):
    """The FIFO key is untouched: renewing is not re-arriving."""
    repo = "acme/fifokey"
    first = await join(client, repo=repo, pr=608, headers=LAPTOP)
    await join(client, repo=repo, pr=609, headers=SERVER)

    for _ in range(3):
        await read(client, repo=repo, pr=608, headers=LAPTOP)

    assert (await read(client, repo=repo, headers=LAPTOP))["active_order"] == [608, 609]
    assert (await _entry(608, repo=repo)).entered_at.isoformat() == first["entry"]["entered"]


async def test_an_overtaken_poll_cannot_pull_an_entry_in(client, monkeypatch):
    """A renewal is a floor under the expiry, not a restatement of it.

    One agent's polls overlap — it is a poll loop, that is what they do — and a
    plain assignment would let the older of two land second and pull the entry in
    by the gap between them. Nothing about "I asked" should ever shorten a window.
    """
    repo = "acme/floor"
    await join(client, repo=repo, pr=612, headers=LAPTOP)
    await read(client, repo=repo, pr=612, headers=LAPTOP)
    stood_at = (await _entry(612, repo=repo)).expires_at

    # The same agent's earlier poll, delayed on the wire, arriving second.
    monkeypatch.setattr(mq, "_utcnow",
                        lambda: datetime.now(UTC) - timedelta(minutes=5))
    late = await read(client, repo=repo, pr=612, headers=LAPTOP)

    assert (await _entry(612, repo=repo)).expires_at == stood_at
    assert late["renewal"]["expires"] == stood_at.isoformat()


# --------------------------------- #405, reconstructed: the night of 2026-08-22


class _Clock:
    """A hand-wound `now` for the endpoint, so a half-hour costs no wall time.

    Every timestamp the queue writes and every comparison it makes goes through
    `merge_queue._utcnow`, so winding this forward is the whole of "time passed" —
    no sleeping, and no TTL shrunk to two seconds, which would test a different
    number from the one that shipped.
    """

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def tick(self, minutes: float) -> datetime:
        self.now += timedelta(minutes=minutes)
        return self.now


#: The line as it stood, in arrival order: the one that was landing, then three
#: that were finished, green and waiting their turn.
_HEAD, _WAITERS = 391, (398, 397, 399)


def _agent(pr: int) -> dict:
    """One agent per PR, each addressable in its own right — the real shape.

    Four sessions on one box all authenticate as that box, so the holder recorded
    on an entry is `laptop/<name>`. It matters here because the whole question is
    whose read renews what.
    """
    return {**LAPTOP, "X-Agent-Key": f"night{pr}"}


async def _the_stalled_night(client, monkeypatch, *, repo: str, renews: bool) -> dict:
    """Replay the measured failure: one landing, three waiting politely.

    `renews=False` is the code as it stood — the read path wrote nothing at all, so
    the only act that could renew an entry was an enqueue, which in practice means
    a push. `renews=True` is this fix. Everything else is identical, including what
    each agent does, which is the point: the difference is not in the agents'
    behaviour but in whether obeying the queue costs them their place.
    """
    if not renews:
        async def _no_renewal(*_args, **_kw):
            return None
        # `raising=False` so this replay also runs against a tree that has no
        # renewal to remove — which is what makes the pair a before/after rather
        # than two readings of the same code. Against the fix, patching it out IS
        # the old behaviour; against the pre-fix tree, the patch is a no-op and the
        # old behaviour is simply what happens.
        monkeypatch.setattr(mq, "_renew_on_read", _no_renewal, raising=False)

    clock = _Clock(datetime.now(UTC))
    monkeypatch.setattr(mq, "_utcnow", clock)

    # 01:12 — four PRs take their places a minute apart, in arrival order. Their
    # windows therefore close a minute apart too: +30, +31, +32, +33.
    line = (_HEAD, *_WAITERS)
    for pr in line:
        await join(client, repo=repo, pr=pr, head=SHA_A, headers=_agent(pr),
                   note=f"landing #{pr}")
        if pr != line[-1]:
            clock.tick(1)
    assert (await read(client, repo=repo))["active_order"] == [_HEAD, *_WAITERS]

    # +12 — the head integrates. This is a push, and a push has ALWAYS renewed:
    # the enqueue that reports the new head moves the expiry out to +42.
    clock.tick(9)
    await join(client, repo=repo, pr=_HEAD, head=SHA_B, headers=_agent(_HEAD),
               note="integrated origin/main, second CI cycle")

    # +22 and +32 — the waiters do the one thing the queue permits them: ask. No
    # rebase, no push, no CI run, exactly as `you.reason` instructs.
    polls: list[dict] = []
    for _ in range(2):
        clock.tick(10)
        for pr in _WAITERS:
            polls.append(await read(client, repo=repo, pr=pr, headers=_agent(pr)))

    # +36 — past every waiter's window and inside the head's, which its
    # integration push moved. This is where the two versions of this code diverge.
    clock.tick(4)
    return {"clock": clock, "view": await read(client, repo=repo), "polls": polls}


async def test_before_the_fix_the_agents_that_obeyed_the_queue_lost_their_places(
        client, monkeypatch):
    """The defect, reconstructed against the code as it stood.

    Three PRs, green and finished, spent half an hour doing precisely what the
    queue told them to do — *"do not rebase, push or restart CI"* — and were
    retired for it. The head, which pushed, kept its place. So the timer was not
    catching a dead agent: it was catching every agent that followed instructions,
    and sparing the one that did the expensive thing.

    Then the compounding, which is what the measurements record. Each lapsed PR
    re-enqueues — that is what its brief says to do — and re-entry is a new
    arrival, so the line comes back in whatever order the agents happened to
    notice. #398, which arrived first among the three and had already integrated,
    is now last, behind two PRs that never did.
    """
    repo = "acme/thenight-before"
    night = await _the_stalled_night(client, monkeypatch, repo=repo, renews=False)

    assert night["view"]["active_order"] == [_HEAD], (
        "every waiter lapsed; the only entry left is the one that pushed")
    assert night["view"]["counts"]["queued"] == 1

    # The polls record the retirement happening. At +22 all three are in the line
    # and are told to sit still. At +32 the first two are already out of it — and
    # the call that told them so is the call that would have kept them in.
    early, late = night["polls"][:3], night["polls"][3:]
    for poll in early:
        assert poll["you"]["queued"] is True
        assert poll["you"]["may_integrate"] is False
    assert [poll["you"]["queued"] for poll in late] == [False, False, True], (
        "windows close in arrival order, so the PR that had waited longest is the "
        "first to be dropped for waiting")

    # And a PR absent from the queue is indistinguishable from one that never
    # joined it, which is the sentence each of them is now told.
    gone = await read(client, repo=repo, pr=398, headers=_agent(398))
    assert gone["you"]["queued"] is False
    assert "not in the queue" in gone["you"]["reason"]

    # Re-entry, in the order the three agents happen to poll next.
    for pr in (399, 397, 398):
        night["clock"].tick(1)
        await join(client, repo=repo, pr=pr, head=SHA_C, headers=_agent(pr),
                   verdict="ready", note="re-joining after my entry lapsed")
    back = await read(client, repo=repo, pr=398, headers=_agent(398))
    assert back["active_order"] == [_HEAD, 399, 397, 398]
    assert back["you"]["position"] == 4, (
        "the PR that arrived first among the waiters, and the only one of the "
        "three that had integrated, is now last in a line it was second in")


async def test_after_the_fix_a_waiter_that_only_asks_keeps_its_place_for_hours(
        client, monkeypatch):
    """The same night, the same agents, the same polite behaviour — and the line
    is still the line.

    Nothing about the agents changed and nothing new was asked of them: the read
    they were already making to find out whether it was their turn is now also what
    holds their place. Half an hour in, past a window every one of them was inside
    when the old code retired them, all four entries stand in arrival order, none
    of them has spent a CI run, and none of them has written anything to the board
    since it joined.

    Then the wait the measurements actually record. #403, #404 and #401 each spent
    between 312 and 327 minutes queued for under an hour of work, so half an hour is
    not the case that matters — a window that survived one landing would lapse ten
    times over across five hours. The second half of this test polls for five of
    them and asserts the line is still the line at the end.
    """
    repo = "acme/thenight-after"
    night = await _the_stalled_night(client, monkeypatch, repo=repo, renews=True)

    assert night["view"]["active_order"] == [_HEAD, *_WAITERS]
    assert night["view"]["counts"]["queued"] == 4
    for poll in night["polls"]:
        assert poll["renewal"]["renewed"] is True
        assert poll["you"]["queued"] is True

    # Positions are the arrival positions, not new ones bought by re-enqueueing.
    for expected, pr in enumerate(_WAITERS, start=2):
        mine = await read(client, repo=repo, pr=pr, headers=_agent(pr))
        assert mine["you"]["position"] == expected
        assert mine["you"]["may_integrate"] is False, (
            "still a waiter: renewal buys a place in the line, never permission")

    # Five more hours of polite waiting — the measured shape of #403 (312 min
    # queued for ~60 min of work), #404 (327) and #401 (332). Nobody pushes,
    # nobody re-enqueues, nobody spends a CI run; they ask, every twenty minutes,
    # and that is the whole of what holds a queue together now.
    clock = night["clock"]
    for _ in range(15):
        clock.tick(20)
        for pr in _WAITERS:
            answer = await read(client, repo=repo, pr=pr, headers=_agent(pr))
            assert answer["renewal"]["renewed"] is True
    assert (await read(client, repo=repo))["active_order"] == list(_WAITERS), (
        "five hours in, the three that kept asking are still in arrival order")
    assert (await _entry(398, repo=repo)).entered_at < clock.now - timedelta(hours=5)

    # The head is gone, and that is the fail-safe working rather than the bug
    # returning: it pushed at +12 and never read the line again, so nothing said it
    # was still there. A landing agent polls like everybody else — this one is a
    # replay of the night, not a model of good behaviour — and the property being
    # shown is that silence, and only silence, retires an entry.
    assert _HEAD not in (await read(client, repo=repo))["active_order"]

    # An hour after their last read the waiters go the same way.
    clock.tick(61)
    assert (await read(client, repo=repo))["active_order"] == []
