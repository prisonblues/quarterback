"""Atomic claims on named resources: who is landing, and who owns a number.

Two issues wanted one primitive. #99: "somebody is landing on ``main`` right
now." #46: "this branch owns v2.31." Both are a claim on a small shared
namespace where a collision is silent until it is expensive, and this repo has
made the case for both the hard way — nine release collisions in two days, the
last three of which killed the cheap remedy.

**Announcing is not claiming, and that is the finding this module exists for.**
Two agents once announced the same version one second apart and were both
correct from what they could see. On 2026-08-16 a number claimed on the board at
10:17 was taken at 11:18 by an agent that picked it by reading ``main`` plus the
open PRs' CHANGELOGs — a check that cannot see a claim which is not in a file —
and the renumber off *that* collision landed on a number claimed seven minutes
earlier. Announcement does not force the next agent to look. Allocation does,
because the number comes from asking.

**Advisory, not a lock, and it must never be described otherwise.** The board
cannot gate github.com: a human merging in the UI, or an agent not enrolled
here, lands regardless. What this removes is collisions between agents that ask,
which is the observed failure mode and the entire claim. The correctness
backstop stays where it was — the pre-land verdict re-checked after base
movement (#96), and CI on ``main``. If a skill ever calls this "the merge lock",
the skill is wrong.

The two kinds ship off one table on purpose. Two independent implementations of
"who has this right now" is the outcome #99 was filed to avoid.
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

from app.auth import identify, reader
from app.db import get_session
from app.identity import same_machine
from app.models.resource_lease import ResourceLease

router = APIRouter(tags=["claim"])

#: Default hold, in seconds. A land takes minutes and a release number is held
#: from "I am writing the CHANGELOG" to "it merged", which on this repo has run
#: to hours. Long enough not to lapse mid-work, short enough that a crashed
#: holder frees it within one coffee.
DEFAULT_TTL = 3600
MAX_TTL = 86_400

#: ``2.31`` or ``v2.31`` or ``2.31.0``. The CHANGELOG's grain is two components
#: (``## v2.31 — …``) and the packaged version's is three (``2.31.0``), so both
#: are accepted and normalised to two. The patch component is deliberately
#: dropped rather than tracked: nothing in this repo has ever allocated one, and
#: a namespace nobody claims in does not need an allocator.
_VERSION_RE = re.compile(r"\Av?(\d{1,4})\.(\d{1,5})(?:\.\d{1,5})?\Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def parse_version(v: str | None) -> tuple[int, int] | None:
    """``"v2.31"`` -> ``(2, 31)``, or None if it is not a version at all.

    None rather than a raise: a caller's ``after`` is a hint about what it could
    see, and a hint this board cannot read must degrade to "you told me nothing"
    rather than costing the caller its allocation. What it must never do is
    become ``(0, 0)`` — that would silently allocate v0.1 over the top of a live
    series.
    """
    if not isinstance(v, str):
        return None
    m = _VERSION_RE.match(v.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def fmt_version(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}"


def _view(c: ResourceLease) -> dict:
    return {
        "claim_id": str(c.id),
        "kind": c.kind,
        "key": c.key,
        "holder": c.holder,
        "session": c.session,
        "note": c.note,
        "acquired": c.acquired_at.isoformat(),
        "expires": c.expires_at.isoformat(),
    }


async def _sweep_lapsed(session: AsyncSession, kind: str, key: str,
                        now: datetime) -> None:
    """Retire a claim whose TTL ran out, so the unique index will admit the next.

    Passive: it runs only when somebody asks for this exact key, so there is no
    reaper and a quiet key costs nothing. ``lapsed`` is set rather than only
    ``released_at`` because "the holder let go" and "the holder vanished" are
    different facts — and for a release number that is the difference between
    shipped and abandoned, which :func:`allocate_release` must not have to guess.
    """
    await session.execute(
        update(ResourceLease)
        .where(ResourceLease.kind == kind, ResourceLease.key == key,
               ResourceLease.released_at.is_(None), ResourceLease.expires_at <= now)
        .values(released_at=now, lapsed=True)
    )


async def _held(session: AsyncSession, kind: str, key: str) -> ResourceLease | None:
    """The outstanding claim on a key, or None. Callers sweep first, so an
    expired row is already released by the time this is asked."""
    return await session.scalar(
        select(ResourceLease)
        .where(ResourceLease.kind == kind, ResourceLease.key == key,
               ResourceLease.released_at.is_(None))
        .limit(1)
    )


async def _take(session: AsyncSession, *, kind: str, key: str, holder: str,
                ttl: int, sess: str | None, note: str | None,
                now: datetime) -> ResourceLease | None:
    """Take the claim, or None if somebody else got there first.

    The atomicity is the partial unique index, NOT this function: two callers
    that both see the key free will both reach the INSERT, and exactly one of
    them commits. Every collision this module exists to stop happened in that
    gap, so it is closed at the database rather than by looking first and hoping.
    """
    claim = ResourceLease(kind=kind, key=key, holder=holder, session=sess,
                          note=note, ttl_seconds=ttl,
                          expires_at=now + timedelta(seconds=ttl))
    session.add(claim)
    try:
        await session.commit()
    except IntegrityError:
        # Somebody committed between our sweep and our insert. Their claim is
        # the real one; roll ours back and let the caller report the holder.
        await session.rollback()
        return None
    await session.refresh(claim)
    return claim


def _conflict(kind: str, key: str, held: ResourceLease) -> HTTPException:
    """409 naming WHO holds it and WHY.

    The refusal is the coordination, not the denial: an agent told only "held"
    can do nothing but retry, and one told "held by zeus/thorn-spruce, landing
    #128, expires 12:04" can go and talk to them or pick up something else.
    """
    return HTTPException(409, detail={
        "error": f"{kind} claim on {key!r} is held",
        "kind": kind, "key": key,
        "held_by": held.holder,
        "session": held.session,
        "note": held.note,
        "acquired": held.acquired_at.isoformat(),
        "expires": held.expires_at.isoformat(),
        "advisory": "this claim is advisory: it cannot stop a merge, only warn you",
    })


class ClaimIn(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=512)
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    session: str | None = None
    #: What you are doing with it. Optional, and worth sending: it is what the
    #: next agent is shown instead of a bare refusal.
    note: str | None = Field(default=None, max_length=500)


class ClaimRefIn(BaseModel):
    claim_id: uuid.UUID


class ReleaseClaimIn(BaseModel):
    repo: str = Field(min_length=1, max_length=256)
    branch: str | None = None
    #: The highest release the CALLER can see, from the repo it has checked out.
    #: The board cannot read a CHANGELOG, so allocation is the maximum of what
    #: the caller knows and what this board has ever handed out — each half
    #: covering the other's blind spot. The caller's repo scan cannot see a claim
    #: that is not yet in a file, which is how the eighth collision happened; the
    #: board cannot see a release that merged without ever being claimed here,
    #: which is every release before this one.
    after: str | None = None
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    session: str | None = None
    note: str | None = Field(default=None, max_length=500)


@router.post("/claim")
async def take_claim(
    body: ClaimIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Claim a named resource, or be told who has it.

    Re-claiming something your own machine already holds is a RENEW, not a
    conflict — the same rule ``POST /lease`` applies, and for the same reason: a
    claim belongs to the box, and an agent that restarts mid-land must be able to
    pick its own claim back up rather than be locked out by its former self.
    """
    now = _utcnow()
    await _sweep_lapsed(session, body.kind, body.key, now)
    await session.commit()

    held = await _held(session, body.kind, body.key)
    if held is not None:
        if not same_machine(held.holder, holder):
            raise _conflict(body.kind, body.key, held)
        held.holder = holder
        held.ttl_seconds = body.ttl
        held.expires_at = now + timedelta(seconds=body.ttl)
        if body.session:
            held.session = body.session
        if body.note:
            held.note = body.note
        await session.commit()
        return {**_view(held), "claimed": True, "renewed": True}

    claim = await _take(session, kind=body.kind, key=body.key, holder=holder,
                        ttl=body.ttl, sess=body.session, note=body.note, now=now)
    if claim is None:
        # Lost the insert race. Re-read rather than reporting a generic failure:
        # the loser of a race is exactly the caller who most needs to know who won.
        winner = await _held(session, body.kind, body.key)
        if winner is None:
            raise HTTPException(409, detail={
                "error": "claim contended; try again",
                "kind": body.kind, "key": body.key})
        if same_machine(winner.holder, holder):
            return {**_view(winner), "claimed": True, "renewed": True}
        raise _conflict(body.kind, body.key, winner)
    return {**_view(claim), "claimed": True, "renewed": False}


@router.post("/claim/renew")
async def renew_claim(
    body: ClaimRefIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    claim = await session.get(ResourceLease, body.claim_id)
    if claim is None:
        raise HTTPException(404, "claim not found")
    if not same_machine(claim.holder, holder):
        raise HTTPException(403, "not your claim")
    if claim.released_at is not None:
        raise HTTPException(409, "claim already released; re-take via POST /claim")
    now = _utcnow()
    if claim.expires_at <= now:
        # Deliberately NOT auto-renewed. The TTL lapsing means another agent may
        # already have taken this key, and silently extending would hand one
        # resource to two holders — which is the whole failure being fixed.
        raise HTTPException(409, "claim expired; re-take via POST /claim")
    claim.expires_at = now + timedelta(seconds=claim.ttl_seconds)
    await session.commit()
    return _view(claim)


@router.post("/claim/release")
async def release_claim(
    body: ClaimRefIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Let go. Idempotent, and never deletes: the row is the history an allocator
    reads, so a released claim stays on the table as a number that was handed
    out."""
    claim = await session.get(ResourceLease, body.claim_id)
    if claim is None:
        raise HTTPException(404, "claim not found")
    if not same_machine(claim.holder, holder):
        raise HTTPException(403, "not your claim")
    if claim.released_at is None:
        claim.released_at = _utcnow()
        await session.commit()
    return {"claim_id": str(claim.id), "released": True, "lapsed": claim.lapsed}


@router.get("/claims")
async def list_claims(
    kind: str | None = None,
    key: str | None = None,
    holder_q: str | None = Query(default=None, alias="holder"),
    include_released: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    _: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What is claimed, by whom, and why.

    Expired-but-unswept rows are filtered out on the way past rather than swept:
    a read must not mutate, and the sweep is the claim path's job. So this view
    and the unique index can briefly disagree about one lapsed row — the index
    still holds it, this says it is gone — and the reader that matters (an agent
    deciding whether to wait) gets the truthful answer, which is that nobody is
    actually there.
    """
    now = _utcnow()
    stmt = select(ResourceLease)
    if kind:
        stmt = stmt.where(ResourceLease.kind == kind)
    if key:
        stmt = stmt.where(ResourceLease.key == key)
    if holder_q:
        stmt = stmt.where(ResourceLease.holder == holder_q)
    if not include_released:
        stmt = stmt.where(ResourceLease.released_at.is_(None),
                          ResourceLease.expires_at > now)
    stmt = stmt.order_by(ResourceLease.acquired_at.desc()).limit(limit)
    rows = list(await session.scalars(stmt))
    return {"claims": [
        {**_view(c),
         "released": c.released_at.isoformat() if c.released_at else None,
         "lapsed": c.lapsed}
        for c in rows]}


# ------------------------------------------------------- the release allocator


def release_key(repo: str, version: tuple[int, int]) -> str:
    return f"{repo}:{fmt_version(version)}"


async def _highest_known(session: AsyncSession, repo: str) -> tuple[int, int] | None:
    """The highest release this board has ever handed out for ``repo``.

    Over EVERY row, released and lapsed included — never only the live ones. A
    number whose claim lapsed is not free: the branch holding it may well have
    shipped, and re-issuing it would manufacture the exact collision this table
    exists to prevent. History is why released rows are kept rather than deleted.
    """
    prefix = f"{repo}:"
    rows = await session.scalars(
        select(ResourceLease.key)
        .where(ResourceLease.kind == "release",
               ResourceLease.key.startswith(prefix))
    )
    seen = [v for v in (parse_version(k[len(prefix):]) for k in rows) if v]
    return max(seen) if seen else None


@router.post("/release/claim")
async def allocate_release(
    body: ReleaseClaimIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Allocate the next free release number for a repo, and hold it.

    #46's expensive half, and the only remedy that survives its own evidence.
    The board hands the number out, so a second branch asking gets the one after
    it without anybody noticing there was a race — where announcing left both
    branches correct and both wrong.

    **Both inputs are needed and neither is sufficient.** The board cannot read a
    CHANGELOG, so it cannot know about releases that merged before it existed;
    the caller's repo scan cannot see a claim that is not yet in any file, which
    is how v2.28 was taken an hour after it was announced. The allocation is the
    maximum of the two, plus one.

    Retries on a lost race rather than failing: losing means somebody just took
    the number this caller was about to, so the correct answer is the next one,
    not an error. Bounded, because an unbounded retry against a genuinely
    contended key is a spin.
    """
    now = _utcnow()
    told = parse_version(body.after)
    unreadable = body.after is not None and told is None

    # Idempotency, checked BEFORE allocating rather than as a renew inside the
    # loop. The loop's candidate is always `highest + 1`, so a number this
    # session already holds is never the candidate and the renew branch below is
    # only reachable on a race — which meant a caller retrying a timed-out
    # request quietly spent a second number. Asked here instead, where the answer
    # is knowable.
    #
    # Scoped to the SESSION and not the machine: see the loop for why that
    # distinction is the whole endpoint.
    if body.session:
        mine = await session.scalar(
            select(ResourceLease)
            .where(ResourceLease.kind == "release",
                   ResourceLease.key.startswith(f"{body.repo}:"),
                   ResourceLease.session == body.session,
                   ResourceLease.released_at.is_(None),
                   ResourceLease.expires_at > now)
            .order_by(ResourceLease.acquired_at.desc())
            .limit(1)
        )
        if mine is not None:
            got = parse_version(mine.key[len(body.repo) + 1:])
            return {**_view(mine),
                    "version": fmt_version(got) if got else None,
                    "claimed": True, "renewed": True,
                    "after_unreadable": unreadable}

    for _attempt in range(8):
        known = await _highest_known(session, body.repo)
        floor = max([v for v in (told, known) if v is not None], default=(0, 0))
        candidate = (floor[0], floor[1] + 1)
        key = release_key(body.repo, candidate)

        await _sweep_lapsed(session, "release", key, now)
        await session.commit()
        held = await _held(session, "release", key)
        if held is not None:
            # **The same-machine renew rule of `POST /claim` must NOT apply here,
            # and a concurrent test is what proved it.** Four callers racing for
            # one repo's numbers came back `3.1, 3.2, 3.3, 3.2` — the duplicate
            # being two agents on one box, where the second found the first's
            # claim, matched on machine and "renewed" into a number that was
            # already spoken for. That is the exact defect this endpoint exists
            # to remove, reintroduced by a convenience borrowed from the wrong
            # kind of claim.
            #
            # The two kinds genuinely differ. For a merge claim, a box re-taking
            # its own claim is an agent recovering from a restart. For a release
            # number, two agents on one machine are two BRANCHES — and this fleet
            # runs several agents per box authenticating as that box, which is
            # precisely the population the allocator is for.
            #
            # Idempotency is keyed on the SESSION instead, so a caller retrying a
            # timed-out request gets its own number back while its co-tenant does
            # not. No session, no renew: a repeat call spends a number, and a
            # skipped number costs nothing while a duplicated one costs a rename
            # across eight files.
            if body.session and held.session and body.session == held.session:
                return {**_view(held), "version": fmt_version(candidate),
                        "claimed": True, "renewed": True,
                        "after_unreadable": unreadable}
            # Held, so it is not free however the arithmetic came out.
            # `_highest_known` will now see it and the next pass moves on.
            continue

        note = body.note or (f"held for {body.branch}" if body.branch else None)
        claim = await _take(session, kind="release", key=key, holder=holder,
                            ttl=body.ttl, sess=body.session, note=note, now=now)
        if claim is None:
            continue
        return {**_view(claim), "version": fmt_version(candidate),
                "claimed": True, "renewed": False,
                # Said rather than swallowed: an `after` this board could not
                # parse means the allocation rested on board history alone, and a
                # caller that mistyped its own version wants to know that before
                # it writes the number into eight files.
                "after_unreadable": unreadable}

    raise HTTPException(409, detail={
        "error": "could not allocate a release number: the namespace is contended",
        "repo": body.repo,
        "advice": "retry; several agents are allocating for this repo right now"})


@router.get("/releases")
async def list_releases(
    repo: str,
    limit: int = Query(default=200, ge=1, le=1000),
    _: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every number this board has handed out for a repo, and who has it.

    Also the answer to a question the board could not previously answer at all —
    "what is landing soon" — which #46 names as a side benefit and is arguably
    the more useful half day to day.
    """
    now = _utcnow()
    prefix = f"{repo}:"
    rows = list(await session.scalars(
        select(ResourceLease)
        .where(ResourceLease.kind == "release", ResourceLease.key.startswith(prefix))
        .order_by(ResourceLease.acquired_at.desc())
        .limit(limit)
    ))
    out = []
    for c in rows:
        v = parse_version(c.key[len(prefix):])
        out.append({
            "version": fmt_version(v) if v else None,
            "holder": c.holder,
            "session": c.session,
            "note": c.note,
            "acquired": c.acquired_at.isoformat(),
            "expires": c.expires_at.isoformat(),
            "held": c.released_at is None and c.expires_at > now,
            "released": c.released_at.isoformat() if c.released_at else None,
            "lapsed": c.lapsed,
        })
    highest = await _highest_known(session, repo)
    return {"repo": repo, "releases": out,
            # What the NEXT call would allocate, absent a higher `after` from the
            # caller. Advisory and racy by nature — reading it is not claiming it,
            # which is the distinction this whole module is about.
            "highest_known": fmt_version(highest) if highest else None}
