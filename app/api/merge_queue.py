"""The line to land on a branch: who is next, who is waiting, and why — #227.

``kind='merge'`` (#99) is a one-slot advisory claim meaning *somebody is landing
on this branch right now*. It answers exactly one question and #227 is about the
four it cannot: which PR is next, who is second, which ready PRs should wait, and
whether the agent about to spend twenty minutes of CI is anywhere near the front.

Without those answers every review-clean PR behaves as though it were next. It
merges the base, pushes, waits for CI, re-runs preland, discovers somebody else
landed, and does it again — #80's quadratic integration cost, plus a failure mode
of its own: each loser's integration push invalidates the winners' green checks on
the way past. #278 stopped a *distant* integration throwing away a review;
nothing stopped five agents each racing to be the one who integrates.

**This is ordering and visibility around the claim, not a second lock.** No path
in this module takes, renews or releases a ``kind='merge'`` claim, and no path
refuses one. Being at the head of the queue is not permission to merge — it is
permission to go and ask for the claim, which may still be held by somebody who
never enqueued at all, and :func:`_claim_view` reports that holder rather than
pretending the queue outranks them. Two implementations of "who has this right
now" is the outcome #99 was filed to avoid, and a queue that also held the
resource would have been the second one.

**Strict FIFO, and only FIFO.** ``GET /merge-queue`` reports ``active_order`` and
a permanently null ``suggested_order``. Every richer input the issue lists — file
overlap from #82, PR size, risk flags, plan dependencies — and the
``order-proposal`` / ``reorder`` endpoints that would carry them are deliberately
not here, because #227's own argument against them is the strongest thing in it:
*"agents may propose order; they must not silently rewrite the queue while also
trying to land… otherwise the queue itself becomes another shared resource every
agent thrashes."* A deterministic arrival order cannot thrash, and it is the only
kind of order that can be shipped before the machinery that decides which
proposals are accepted. #227 stays open for that half.

**The board takes testimony, not measurements.** It cannot run preland, read CI or
ask GitHub whether a PR is a draft. What ``verdict`` and ``head`` do is pin the
caller's claim about its own PR to a specific commit — so the claim expires by
itself when the branch moves, which is the one thing an agent's own memory of
"I was ready" structurally cannot do.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import clean_session, is_unique_violation, live_claim
from app.auth import identify, reader
from app.claimkey import BadRef, derive
from app.db import get_session
from app.models.merge_queue import PROCEEDS, VERDICTS, MergeQueueEntry
from app.models.resource_lease import ResourceLease

router = APIRouter(tags=["merge-queue"])

#: How long an entry survives without being renewed. Much shorter than a claim's
#: hour, and on purpose: a lapsed claim frees a resource nobody is using, while a
#: lapsed queue entry lets everybody behind it move. The cost of getting this
#: wrong is asymmetric — an agent that is still working re-enqueues on its next
#: poll and keeps its place (``entered_at`` is never bumped), whereas a head that
#: died holds the whole line for however long this is.
DEFAULT_TTL = 1800
MAX_TTL = 86_400

#: The longest session identifier that means anything — the claim table's bound,
#: for the same reason it has one.
MAX_SESSION = 200

#: A git object name, full length. The rule is
#: :data:`app.api.reviews._SHA_RE`'s and the *trade* is the opposite one: reviews
#: drop a garbled head rather than lose a run's findings, because recording is
#: best-effort there. Here the head is the entire mechanism — an entry is ready
#: exactly while ``ready_sha == head_sha`` — so a value that is not a commit id is
#: refused with a 422. Dropping it would leave an entry whose readiness could
#: never expire, which is a permanent green light rather than a missing field.
_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

#: What preland says when a PR is genuinely not landable, and the one verdict this
#: endpoint refuses to enqueue. Named so the refusal can say it back.
_HOLD = "hold"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _norm_sha(value: str) -> str:
    """A commit id, lower-cased, or a 422. Never a repair."""
    sha = value.strip().lower()
    if not _SHA_RE.match(sha):
        raise HTTPException(422, detail={
            "error": f"{value!r} is not a commit id",
            "hint": "send the PR's full head oid (`gh pr view --json headRefOid`). "
                    "The queue invalidates your readiness when this changes, so a "
                    "value that cannot be compared is a readiness that never expires",
        })
    return sha


def merge_key(repo: str, base: str) -> tuple[str, str]:
    """The ``kind='merge'`` claim this queue is the line for — ``(kind, key)``.

    Derived through :mod:`app.claimkey` rather than composed here, so the queue
    and the claim cannot end up naming the same land two ways. That is #172's
    whole finding, and a new table that spelled the key itself would be the next
    place to reproduce it.

    It also validates: the repo must be ``owner/name`` and the base must be a
    branch name ``git check-ref-format`` would accept, so a queue cannot be opened
    on a ref that cannot exist.

    The base is the branch a *lander* claims, which #318 settled after this landed
    keying on it: ``preland.check_merge_claim`` read the HEAD branch until then, so
    the queue reported a claim at one key while the gate read another and the two
    named one land two ways — the very thing the paragraph above says this function
    exists to prevent. Both read ``<repo>:<base>`` now.
    """
    return derive("branch", repo=repo, value=base)


def _scope(repo: str, base: str) -> tuple[str, str, str]:
    """``(repo, base, key)`` for a queue, validated. Raises 422 like the rest."""
    try:
        _, key = merge_key(repo, base)
    except BadRef as e:
        raise HTTPException(422, str(e)) from None
    # `derive` canonicalises the repo (lower-cased) and leaves the branch alone;
    # read both back off the key rather than re-deriving, so the row stored and
    # the claim looked up cannot disagree by one normalisation.
    canon_repo, _, canon_base = key.partition(":")
    return canon_repo, canon_base, key


async def _sweep_lapsed(session: AsyncSession, repo: str, base: str,
                        now: datetime) -> None:
    """Retire entries whose TTL ran out, so the queue advances past a dead head.

    Passive, exactly as ``app.api.claims._sweep_lapsed`` is: it runs only when
    somebody asks about this queue, so a quiet branch costs nothing and there is
    no reaper to wedge. ``lapsed`` is set as well as ``left_at`` because "landed
    and stood down" and "stopped answering" are different facts about a queue
    head, and a board that showed them alike would report an abandoned land as a
    finished one.

    Called only from the write paths. ``GET`` filters expired rows on the way past
    instead — a read must not mutate, which is the rule ``GET /claims`` already
    keeps.
    """
    await session.execute(
        update(MergeQueueEntry)
        .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
               MergeQueueEntry.left_at.is_(None), MergeQueueEntry.expires_at <= now)
        .values(left_at=now, lapsed=True,
                left_reason="entry lapsed: its holder stopped renewing")
    )


async def _live_entries(session: AsyncSession, repo: str, base: str,
                        now: datetime) -> list[MergeQueueEntry]:
    """The queue, in order: still in, not expired, oldest arrival first.

    ``pr`` breaks a tie on ``entered_at``. Two entries can share a timestamp, and
    an order that then depended on which row the planner returned first would
    report two different heads on two consecutive reads — worse than no queue,
    because both agents would believe they were next.
    """
    return list((await session.scalars(
        select(MergeQueueEntry)
        .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
               MergeQueueEntry.left_at.is_(None), MergeQueueEntry.expires_at > now)
        .order_by(MergeQueueEntry.entered_at, MergeQueueEntry.pr)
    )).all())


def is_ready(entry: MergeQueueEntry) -> bool:
    """Does the board hold a ``ready`` verdict about the commit this entry is on?

    Both halves matter and the check constraint keeps them together: an entry may
    only carry the proceeding verdict while ``ready_sha`` equals ``head_sha``. An
    agent remembers "preland said READY" and does not reliably notice that the
    thing preland said it about was three pushes ago; a row cannot forget which
    commit it was talking about.
    """
    return entry.verdict == PROCEEDS and entry.ready_sha == entry.head_sha


def _position(entries: list[MergeQueueEntry], pr: int) -> int | None:
    """Where this PR is in the line, or None if it is not in it.

    Total on purpose. The obvious spelling — ``[e.pr for e in entries].index(pr)``
    — raises when the entry is not there, and "not there" is reachable: a
    concurrent ``leave`` can retire an entry between the write and the read that
    renders it, and a 500 out of a successful enqueue is the worst of both
    answers.
    """
    for i, e in enumerate(entries, start=1):
        if e.pr == pr:
            return i
    return None


def entry_view(e: MergeQueueEntry, position: int | None) -> dict:
    return {
        "entry_id": str(e.id),
        "pr": e.pr,
        "position": position,
        "head": e.head_sha,
        "ready_sha": e.ready_sha,
        "verdict": e.verdict,
        "ready": is_ready(e),
        "holder": e.holder,
        "session": e.session,
        "note": e.note,
        "entered": e.entered_at.isoformat(),
        "updated": e.updated_at.isoformat(),
        "expires": e.expires_at.isoformat(),
    }


def _claim_view(claim: ResourceLease | None, key: str) -> dict:
    """Who holds the land right now, read-only.

    The queue reports this and never acts on it. An agent at the head of the line
    that finds the claim held has learned something useful (go and talk to that
    holder) and gained no authority whatsoever — a human merging in the UI, or an
    agent that never enqueued, lands regardless, which is the same advisory
    boundary ``app.api.claims`` insists on and must not be softened by a table
    that looks more official.
    """
    if claim is None:
        return {"key": key, "held": False, "holder": None, "session": None, "note": None}
    return {
        "key": key,
        "held": True,
        "claim_id": str(claim.id),
        "holder": claim.holder,
        "session": claim.session,
        "note": claim.note,
        "expires": claim.expires_at.isoformat(),
    }


def decide(entries: list[MergeQueueEntry], pr: int,
           at_head: str | None = None) -> dict:
    """What may this PR do right now, and why — the whole point of the queue.

    Returns ``may_integrate`` and ``may_merge`` rather than one verdict, because
    they are different permissions and collapsing them is exactly what the
    behaviour this replaces gets wrong. A non-head may do neither: it must not
    rebase, push or restart CI, because all three cost a real CI run to discover
    something the board already knew, and the push also invalidates the head's
    green checks. The head may integrate — that is what its slot is for — but may
    only merge while the board holds a ``ready`` verdict about the commit the PR
    is actually on.

    ``at_head`` is the caller's own reading of the PR's current head, and it is
    how a head change invalidates readiness without anything being written: a
    caller that has just asked GitHub, or a peer checking on somebody else's
    entry, passes it and is told the entry is behind the branch. Omitting it
    means "judge the entry as it stands", which is the honest answer for a caller
    that does not know.

    ``reason`` is populated on the yes as well as the no. An agent that is allowed
    to proceed still has to be able to say why on the board, and a caller that
    only ever logs refusals learns nothing about the grants.
    """
    order = [e.pr for e in entries]
    if pr not in order:
        return {
            "queued": False, "position": None, "is_head": False,
            "may_integrate": False, "may_merge": False,
            "reason": (f"#{pr} is not in the queue for this base — enqueue it "
                       f"before landing, so everyone else can see the line"),
            "waiting_on": None,
        }
    position = order.index(pr) + 1
    entry = entries[position - 1]
    # Position is checked FIRST, before anything about readiness. A non-head that
    # was told "your head moved" would go and do the one thing this queue exists
    # to stop it doing: push, and burn a CI run, while not being next.
    if position > 1:
        ahead = entries[0]
        return {
            "queued": True, "position": position, "is_head": False,
            "may_integrate": False, "may_merge": False,
            "reason": (f"queued behind #{ahead.pr}, position {position} of "
                       f"{len(order)} — do not rebase, push or restart CI: you "
                       f"would spend a run to learn what this line already says, "
                       f"and invalidate #{ahead.pr}'s checks doing it"),
            "waiting_on": {"pr": ahead.pr, "holder": ahead.holder,
                           "session": ahead.session, "note": ahead.note},
        }
    if at_head is not None and at_head != entry.head_sha:
        return {
            "queued": True, "position": 1, "is_head": True,
            # Integrating stays allowed: it is how a head gets back to ready, and
            # the head has already pushed anyway — this is reporting the state, not
            # granting a new permission.
            "may_integrate": True, "may_merge": False,
            "reason": (f"#{pr} is the head, but it has moved to {at_head[:12]} "
                       f"since it enqueued at {entry.head_sha[:12]}: re-run "
                       f"preland against this head and re-enqueue before merging"),
            "waiting_on": None,
        }
    if not is_ready(entry):
        return {
            "queued": True, "position": 1, "is_head": True,
            "may_integrate": True, "may_merge": False,
            "reason": (f"#{pr} is the head at {entry.head_sha[:12]} with verdict "
                       f"{entry.verdict!r}: integrate with the base if you need to, "
                       f"then re-run preland and re-enqueue as {PROCEEDS!r} — the "
                       f"board holds no ready verdict for this commit"),
            "waiting_on": None,
        }
    return {
        "queued": True, "position": 1, "is_head": True,
        "may_integrate": True, "may_merge": True,
        "reason": (f"#{pr} is the head of the queue and ready at "
                   f"{entry.head_sha[:12]}. Being head is not the claim: take "
                   f"`kind=merge` on this base before you merge"),
        "waiting_on": None,
    }


class EnqueueIn(BaseModel):
    """Join the line, or renew and update the place you already have."""

    repo: str = Field(min_length=1, max_length=256, description="`owner/name`")
    base: str = Field(min_length=1, max_length=256,
                      description="the branch being landed ONTO")
    pr: int = Field(ge=1, description="the pull request number")
    #: The PR's head oid, full length. Required, and the reason it cannot be
    #: optional is :func:`is_fresh`: an entry with no head has a readiness that
    #: never expires.
    head: str = Field(min_length=7, max_length=64)
    #: What preland said about ``head``. Default ``queued`` is the honest one for
    #: an agent that has been refused by this endpoint and is re-registering: it
    #: is admissible, and it does not let anything merge.
    verdict: str = Field(default="queued",
                         description=f"one of: {', '.join(VERDICTS)}")
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    note: str | None = Field(default=None, max_length=500,
                             description="what you are landing — read by everyone behind you")


class LeaveIn(BaseModel):
    repo: str = Field(min_length=1, max_length=256)
    base: str = Field(min_length=1, max_length=256)
    pr: int = Field(ge=1)
    #: WHICH place in the line, when the caller knows — every enqueue response
    #: carries it. A PR number names a pull request, not one of its stays in the
    #: queue, so a leave that arrives after the PR left and re-joined would
    #: otherwise retire the new entry while meaning the old one. The timestamp
    #: guard below catches that only when the two overlap at the server; a leave
    #: delayed in transit gets a fresh timestamp on arrival and is
    #: indistinguishable from a prompt one. This is the exact identification, and
    #: it is optional rather than required so a caller that never held the id
    #: (retiring a peer's abandoned entry) can still stand it down.
    entry_id: uuid.UUID | None = None
    #: Required. The queue advancing is the moment everybody behind starts
    #: spending CI, and "the entry vanished" with no why makes that unauditable —
    #: the same argument the claim table's ``note`` already carries, one step
    #: further because leaving affects other agents rather than just the leaver.
    reason: str = Field(min_length=1, max_length=500,
                        description="merged / closed / superseded / abandoned")


def _admit(verdict: str) -> str:
    """The verdict, or a 422 refusing entry — the gate on the front of the line.

    A PR that is genuinely blocked does not belong in a queue: it would sit at the
    head holding everybody up until its TTL ran out, having never been able to
    land. So preland ``HOLD`` is named and refused rather than folded into
    ``queued``, and an unknown verdict is refused rather than guessed at, for the
    reason :data:`app.models.merge_queue.VERDICTS` gives.
    """
    v = (verdict or "").strip().lower()
    if v in VERDICTS:
        return v
    detail = {
        "error": f"{verdict!r} is not a state that admits a PR to the queue",
        "verdicts": list(VERDICTS),
    }
    if v == _HOLD:
        detail["hint"] = (
            "preland HOLD means something is wrong with the PR itself, not with "
            "its turn. An entry at the head that can never land holds the line "
            "until its TTL expires — fix the objection, then enqueue")
    else:
        detail["hint"] = (
            "`ready` is preland READY, `reconcile` is preland RECONCILE (a stale "
            "base, which landing in turn dissolves), and `queued` means the only "
            "objection is your position in this line")
    raise HTTPException(422, detail=detail)


@router.get("/merge-queue")
async def read_queue(
    repo: str = Query(..., min_length=1, description="`owner/name`"),
    base: str = Query(..., min_length=1, description="the branch being landed onto"),
    pr: int | None = Query(default=None, ge=1,
                           description="also answer `you`: what may THIS pr do right now"),
    head: str | None = Query(default=None, min_length=7, max_length=64,
                             description="`pr`'s head oid as YOU see it — how a head "
                                         "change invalidates readiness without a write"),
    _: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The line for one base: who is next, who is waiting, and what each waits on.

    ``suggested_order`` is always ``null`` and that is not an omission — it is the
    distinction #227 asks for, present from the first cut so a later ordering
    proposal has somewhere to go that is visibly *not* the live order. Nothing in
    this release can populate it.

    Expired-but-unswept entries are filtered out on the way past rather than swept,
    because a read must not mutate — the rule ``GET /claims`` keeps. So this view
    and the unique index can briefly disagree about one lapsed row, and the reader
    that matters (an agent deciding whether it may move) gets the truthful answer.
    """
    canon_repo, canon_base, key = _scope(repo, base)
    now = _utcnow()
    entries = await _live_entries(session, canon_repo, canon_base, now)
    claim = await live_claim(session, "merge", key, now)
    out = {
        "repo": canon_repo,
        "base": canon_base,
        "generated": now.isoformat(),
        "ordering": "fifo",
        "active_order": [e.pr for e in entries],
        # Not a placeholder for something this endpoint sometimes fills in: see
        # the module docstring on why ordering proposals are #227's second half.
        "suggested_order": None,
        "note_on_ordering": (
            "strict FIFO by arrival (#227, first cut). Ordering proposals — file "
            "overlap, size, risk, plan dependencies — are not implemented, and "
            "nothing here mutates the order automatically"),
        "head": entry_view(entries[0], 1) if entries else None,
        "entries": [entry_view(e, i) for i, e in enumerate(entries, start=1)],
        "claim": _claim_view(claim, key),
        "note_on_claim": (
            "the queue is ordering around the `kind=merge` claim, not a second "
            "lock: being at the head does not hold this claim, and this claim "
            "being held by someone who never enqueued is normal and advisory"),
        "counts": {
            "queued": len(entries),
            "ready": sum(1 for e in entries if is_ready(e)),
            "not_ready": sum(1 for e in entries if not is_ready(e)),
        },
    }
    if pr is not None:
        out["you"] = decide(entries, pr, at_head=_norm_sha(head) if head else None)
    elif head is not None:
        # A head with no PR names nothing. Refused rather than ignored: a caller
        # that believed it was asking "is my entry stale" and silently got the
        # unconditioned answer is the failure this parameter exists to prevent.
        raise HTTPException(422, "`head` says which commit `pr` is on; send `pr` too")
    return out


@router.post("/merge-queue/enqueue")
async def enqueue(
    body: EnqueueIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take a place in the line, or update the one you have. Idempotent.

    Idempotent in the strong sense: a second call for a PR already queued updates
    that row — head, verdict, note, expiry — and **leaves ``entered_at`` alone**,
    so re-registering never costs a place. An agent that has just been refused
    ("queued behind #123") is meant to call this again on its next poll, and a
    queue that sent it to the back for doing so would be a queue nobody could
    safely poll.

    The board is taking your word for it. It cannot run preland, read CI or ask
    GitHub whether this PR is a draft, so ``verdict`` is testimony — what it adds
    is that the testimony is pinned to ``head`` and stops counting the moment the
    branch moves.
    """
    canon_repo, canon_base, key = _scope(body.repo, body.base)
    head = _norm_sha(body.head)
    verdict = _admit(body.verdict)
    now = _utcnow()
    sess = clean_session(body.session)

    await _sweep_lapsed(session, canon_repo, canon_base, now)
    await session.commit()

    entry = await _join(session, canon_repo, canon_base, body.pr,
                        _row(head=head, verdict=verdict, holder=holder, sess=sess,
                             note=body.note, ttl=body.ttl, now=now), now)

    entries = await _live_entries(session, canon_repo, canon_base, now)
    claim = await live_claim(session, "merge", key, now)
    return {
        "repo": canon_repo,
        "base": canon_base,
        "entry": entry_view(entry, _position(entries, entry.pr)),
        "active_order": [e.pr for e in entries],
        "you": decide(entries, entry.pr),
        "claim": _claim_view(claim, key),
    }


def _row(*, head: str, verdict: str, holder: str, sess: str | None,
         note: str | None, ttl: int, now: datetime) -> dict:
    """The columns an enqueue writes, whether it is inserting or renewing.

    One dict for both paths, so the insert and the update cannot come to mean
    different things — a re-enqueue that set a column the first enqueue did not
    is how "calling this twice is safe" quietly stops being true.

    ``entered_at`` is conspicuously absent: it is the FIFO key, written once by
    the insert and never again. Everything else moves, including ``holder`` — a
    PR handed to another agent mid-land keeps its place, because the place
    belongs to the pull request and not to whoever is driving it this hour.

    ``ready_sha`` follows ``head`` **only** when the call asserts the proceeding
    verdict at that head; every other verdict clears it. So reporting a new head
    without re-running preland invalidates the readiness rather than carrying it
    forward onto a commit nobody checked, which is #227's "when a queued PR's head
    changes, its readiness is invalidated until preland is rerun against that
    head" — and it holds whether the head moved by one commit or by fifty. The
    check constraint says the same thing from the other side, so the two cannot
    drift.

    ``session`` and ``note`` are written only when sent, because a poll that
    omits them means "unchanged", not "cleared" — and clearing the note would
    blank the one line everyone queued behind this entry is reading.
    """
    values: dict = {
        "head_sha": head,
        "verdict": verdict,
        "ready_sha": head if verdict == PROCEEDS else None,
        "holder": holder,
        "ttl_seconds": ttl,
        "expires_at": now + timedelta(seconds=ttl),
        "updated_at": now,
    }
    if sess:
        values["session"] = sess
    if note is not None:
        values["note"] = note
    return values


async def _join(session: AsyncSession, repo: str, base: str, pr: int,
                values: dict, now: datetime) -> MergeQueueEntry:
    """Take or renew this PR's place. Decided by the database, never by looking first.

    Two attempts, and the second is not a retry-and-hope: each attempt can lose in
    exactly one way, and the loss says which branch to take next.

    * The INSERT loses to ``ix_merge_queue_open`` when somebody enqueued this PR
      in the gap after the SELECT. Their row is the real one, so the next pass
      renews it — this endpoint's whole contract is that calling it twice is safe.
    * The UPDATE is conditional and can lose two ways. ``left_at IS NULL`` loses
      when a concurrent ``leave`` or sweep retired the row in that same gap —
      read-then-write would have stamped a fresh expiry onto a departed entry,
      which is worse than failing: it resurrects nothing while reporting success,
      and the response then cannot find its own entry in the live queue.
      ``updated_at <= now`` loses to a *newer* enqueue that overtook this one,
      and there the newer row is simply the answer: returning it is truthful,
      where overwriting it would put a stale ``ready`` verdict back onto a commit
      the PR has moved off.

    So each loss names the next step, and two passes is exactly enough. A third
    would mean two writers are trading the row back and forth, which they should
    not be: one PR is driven by one agent, and the only contention expected here
    is that agent's own overlapping polls.
    """
    for _ in range(2):
        existing = await session.scalar(
            select(MergeQueueEntry).where(
                MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
                MergeQueueEntry.pr == pr, MergeQueueEntry.left_at.is_(None))
        )
        if existing is None:
            # Did this PR leave AFTER this request started? Then this request is
            # an obsolete poll — in flight when somebody stood the entry down —
            # and inserting would resurrect a PR that has merged, at the back of
            # a line it has no business being in. It would expire on its own, but
            # a stale record of a claim nobody is making is worse than none: it
            # is a second answer to a question that already has one.
            #
            # A leave that happened BEFORE this request is the ordinary re-join,
            # and goes to the back exactly as it should. The comparison is what
            # separates the two, and both timestamps come from one server clock.
            departed = await session.scalar(
                select(MergeQueueEntry)
                .where(MergeQueueEntry.repo == repo, MergeQueueEntry.base == base,
                       MergeQueueEntry.pr == pr, MergeQueueEntry.left_at > now)
                .order_by(MergeQueueEntry.left_at.desc()).limit(1)
            )
            if departed is not None:
                raise HTTPException(409, detail={
                    "error": f"#{pr} left the queue after this request started",
                    "repo": repo, "base": base, "pr": pr,
                    "left_at": departed.left_at.isoformat(),
                    "left_by": departed.left_by,
                    "left_reason": departed.left_reason,
                    "hint": "your request was in flight when the entry was stood "
                            "down. If this PR really is still landing, enqueue "
                            "again — it will join at the back, which is where a "
                            "PR that left the line belongs",
                })
            entry = MergeQueueEntry(repo=repo, base=base, pr=pr,
                                    entered_at=now, **values)
            session.add(entry)
            try:
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                # Only the idempotency index. Any other integrity failure — a
                # check constraint, say — is a real fault, and reporting it as
                # "you were already queued" would send the caller looking at a row
                # that does not exist.
                if not is_unique_violation(e):
                    raise
                continue
            await session.refresh(entry)
            return entry
        existing_id = existing.id
        done = await session.execute(
            update(MergeQueueEntry)
            .where(MergeQueueEntry.id == existing_id,
                   MergeQueueEntry.left_at.is_(None),
                   # Monotonic. Two enqueues for one PR can be in flight at once
                   # — an agent that pushed and re-registered while its previous
                   # poll was still on the wire — and last-writer-wins would let
                   # the older one land second and put a `ready` verdict back on
                   # a commit the PR has moved off. That is precisely the stale
                   # green light this whole feature exists to remove, so the
                   # older request loses at the database rather than by arriving
                   # first. All requests are stamped by one server clock, so
                   # there is no skew to compare across.
                   MergeQueueEntry.updated_at <= now)
            .values(**values)
            .returning(MergeQueueEntry.id)
        )
        if done.scalar_one_or_none() is None:
            await session.rollback()
            # Which guard refused it? The answers are different, so the test is
            # the guard's own predicate rather than "is the row still there" —
            # otherwise a row that merely lost a lap to contention would be
            # reported as somebody else's newer state.
            current = await session.get(MergeQueueEntry, existing_id)
            if (current is not None and current.left_at is None
                    and current.updated_at > now):
                # A NEWER enqueue won. Its answer is the true one: return it
                # rather than clobbering it, and rather than refusing a caller
                # whose PR is correctly registered — by somebody holding fresher
                # information about it, which is usually itself.
                return current
            # Retired in the gap, or simply contended. Either way the next pass
            # re-reads and decides again: if it left, coming back is a new
            # arrival, which is honest.
            continue
        await session.commit()
        fresh = await session.get(MergeQueueEntry, existing_id)
        if fresh is not None:
            return fresh
    raise HTTPException(409, detail={
        "error": "queue entry contended; try again",
        "repo": repo, "base": base, "pr": pr,
        "hint": "this PR was entering and leaving the queue at the same moment. "
                "One PR is driven by one agent, so if this repeats, two of your "
                "sessions are landing the same branch",
    })


@router.post("/merge-queue/leave")
async def leave_queue(
    body: LeaveIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stand down, and let everybody behind you move. Idempotent.

    **Any authenticated agent may retire any entry, and that is deliberate.** The
    case this endpoint exists for is a head that merged, closed or was abandoned,
    and in every one of those the agent best placed to notice is somebody else —
    the one sitting at position 2 watching a PR that is already merged hold the
    line. Restricting it to the owner would leave the TTL as the only way out of
    exactly the situation the TTL is the crude fallback for.

    What guards it is the record rather than a refusal: ``left_by`` and
    ``left_reason`` are both stored, ``reason`` is required, and the row is never
    deleted — so an entry retired out from under its owner is visible as such
    afterwards. That is the trade the claim table makes with ``note``, taken one
    step further because leaving affects other agents and not only the leaver.

    Send ``entry_id`` when you have one — the enqueue that put you in the line
    returned it. A PR number names a pull request rather than one of its stays in
    the queue, and the difference matters exactly once: when the PR left and
    re-joined between your decision to stand down and this call arriving.

    It does **not** touch the ``kind=merge`` claim. An agent that held one releases
    it through ``POST /claim/release``; a queue that released claims on its own
    would be the second implementation of the claim this module exists not to be.
    """
    canon_repo, canon_base, key = _scope(body.repo, body.base)
    now = _utcnow()
    await _sweep_lapsed(session, canon_repo, canon_base, now)
    await session.commit()

    # Conditional UPDATE, not read-then-write: a concurrent sweep can retire this
    # row between a read and a write, and re-stamping it would overwrite `lapsed`
    # — turning "its holder stopped answering" into "it stood down cleanly",
    # which is the one distinction the column exists to keep.
    # The entry this leave is ABOUT, not merely one carrying the same PR number.
    # A PR that left, was reworked and re-enqueued must not be dropped back out
    # of the line by the tidy-up for its predecessor — the one failure a queue
    # cannot have. `entry_id` is the exact answer when the caller has one, and
    # `entered_at <= now` is the fallback: it separates the two incarnations
    # whenever they overlap at the server, which is the case a caller that never
    # held an id can actually be in.
    scoped = ((MergeQueueEntry.id == body.entry_id,) if body.entry_id is not None
              else (MergeQueueEntry.entered_at <= now,))
    left = await session.execute(
        update(MergeQueueEntry)
        .where(MergeQueueEntry.repo == canon_repo, MergeQueueEntry.base == canon_base,
               MergeQueueEntry.pr == body.pr, MergeQueueEntry.left_at.is_(None),
               *scoped)
        .values(left_at=now, left_by=holder, left_reason=body.reason.strip(),
                updated_at=now)
        .returning(MergeQueueEntry.id)
    )
    entry_id: uuid.UUID | None = left.scalar_one_or_none()
    await session.commit()

    entries = await _live_entries(session, canon_repo, canon_base, now)
    claim = await live_claim(session, "merge", key, now)
    return {
        "repo": canon_repo,
        "base": canon_base,
        "pr": body.pr,
        # False when the entry was already gone — swept, stood down by a peer,
        # or replaced by a later arrival this leave is not about. Not a 404: "the
        # entry you meant is not in the queue" is the state the caller wanted,
        # and an agent tidying up after a merge should not have to care whether
        # the TTL beat it to it.
        "left": entry_id is not None,
        "entry_id": str(entry_id) if entry_id is not None else None,
        "reason": body.reason.strip(),
        "active_order": [e.pr for e in entries],
        "head": entry_view(entries[0], 1) if entries else None,
        "claim": _claim_view(claim, key),
    }
