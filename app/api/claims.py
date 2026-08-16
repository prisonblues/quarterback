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


#: Kinds the generic ``POST /claim`` refuses. ``release`` carries invariants no
#: generic claim can honour — the number is never re-issued, and the allocation
#: floor only ever rises — and those are enforced in :func:`allocate_release`
#: alone. Left open, a caller could take `kind='release'` on an already-released
#: historical key (re-issuing a shipped number), advance the floor forever with
#: `key='<repo>:9999.1'`, or insert `v2.31` beside a held `2.31` — an alternate
#: spelling the unique index cannot see, leaving two agents each certain they
#: hold "the same" number. Round 1's F01.
RESERVED_KINDS = frozenset({"release"})

#: The largest minor component :data:`_VERSION_RE` can read back. Allocation is
#: `minor + 1` with unbounded Python arithmetic, so without this a repo at
#: `9999.99999` would be handed `9999.100000` — a string the parser rejects,
#: which makes it invisible to `_highest_known` and hands the SAME number to
#: every caller thereafter. Round 1's F17: the allocator's own output has to stay
#: inside the grammar the allocator reads.
MAX_MINOR = 99_999
MAX_MAJOR = 9_999


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clean(s: str | None) -> str | None:
    """A caller-supplied identifier, or None — never the empty string.

    ``session=""`` used to be stored on the first claim and then skipped by every
    idempotency lookup, because the lookups test truthiness. Each retry therefore
    consumed a fresh number while reporting success (round 1's F27). One
    normalisation at the edge, so no downstream test has to remember the
    difference between absent and blank.
    """
    if not isinstance(s, str):
        return None
    return s.strip() or None


def _next_version(floor: tuple[int, int]) -> tuple[int, int] | None:
    """The version after ``floor``, or None if the namespace is exhausted.

    Rolls the major when the minor would leave what :data:`_VERSION_RE` can read
    back, because an allocated number the allocator cannot re-parse is worse than
    a refusal: it disappears from `_highest_known` and every later caller is
    handed it again.
    """
    major, minor = floor
    if minor + 1 <= MAX_MINOR:
        return (major, minor + 1)
    return (major + 1, 0) if major + 1 <= MAX_MAJOR else None


def _may_mutate(claim: ResourceLease, holder: str, session_id: str | None) -> bool:
    """May this caller change this claim?

    **The premise round 1 broke, and it was this module's own.** Every mutating
    path authorised with :func:`same_machine` alone, inherited from ``Lease``
    where it is right — a session lease belongs to the box, so an agent
    recovering from a restart must be able to reclaim it. The allocator's own
    comment argues at length that for a release number *"two agents on one
    machine are two BRANCHES"*, and then every renew, release and renumber
    authorised by machine anyway. A co-tenant could silently renumber a branch
    that had already written its version into eight files.

    **That fix named one kind, and the premise was never about kinds (#142).**
    The rule above read ``claim.kind == "release" and claim.session``, so every
    other kind kept the machine-only authorisation it had just been argued out
    of. On a one-box fleet — which is what this fleet is — a co-tenant claiming
    a key another agent holds got ``renewed: true`` rather than a 409: a
    collision with a green light on it, which is worse than no claim at all
    because it reads as authoritative. Measured on 2026-08-16: three agents
    claimed overlapping work inside 56 seconds and a human resolved it by
    reading timestamps off the board.

    **Every kind in THIS table is exclusive work**, which is what makes the
    general rule safe rather than merely tidier. ``same_machine`` is right for a
    session lease — an agent recovering from a restart must reclaim its own —
    and session leases are a different table with their own checks in
    ``app/api/leases.py``; nothing here governs them. So there is no kind left
    for which the machine is the right owner, and no opt-out list is needed.
    (#142 proposed one. Reading the code said it was unnecessary, which is the
    cheaper answer: an opt-out set is a second place to forget something.)

    What survives from the release-only version, because it was right and is not
    specific to releases: the machine is necessary throughout, and a claim that
    named **no** session falls back to the machine — there is nothing finer to
    check, and refusing outright would strand claims taken by callers that sent
    none.
    """
    if not same_machine(claim.holder, holder):
        return False
    if claim.session:
        return _clean(session_id) == claim.session
    return True


def _not_yours(claim: ResourceLease) -> HTTPException:
    return HTTPException(403, detail={
        "error": "not your claim",
        "kind": claim.kind, "key": claim.key,
        "held_by": claim.holder, "session": claim.session,
        "hint": ("a claim is owned by the session that took it, not by the machine: "
                 "two agents on one box are two agents. Take a different key, or "
                 "wait for this one to lapse — its holder and expiry are above"),
    })


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


async def _held(session: AsyncSession, kind: str, key: str,
                now: datetime) -> ResourceLease | None:
    """The claim actually holding a key right now, or None.

    Tests ``expires_at`` as well as ``released_at``. Callers sweep first, but the
    sweep uses a timestamp captured once at request start, so a claim can expire
    strictly after it and still come back from a released_at-only filter — and
    ``POST /claim``'s own-machine branch would then "renew" a lease that had in
    fact lapsed and could already belong to somebody else (round 1's F23).

    The unique index is deliberately broader than this: it cannot test
    ``expires_at`` (a partial predicate must be immutable), so the index may
    still be holding a row this call correctly reports as gone. That gap is what
    the sweep exists to close, and is why the insert path must handle losing.
    """
    return await session.scalar(
        select(ResourceLease)
        .where(ResourceLease.kind == kind, ResourceLease.key == key,
               ResourceLease.released_at.is_(None),
               ResourceLease.expires_at > now)
        .limit(1)
    )


def _repo_prefix(repo: str):
    """A LIKE clause matching one repo's release keys, with wildcards escaped.

    ``startswith`` compiles to ``LIKE 'prefix%'`` and does NOT escape ``_`` or
    ``%``, both of which are LIKE wildcards and both of which occur in real repo
    names. ``acme/my_repo`` matched ``acme/myXrepo`` (round 1's F19) — so one
    repo's allocation floor could be raised by another's, and `/releases` could
    list a neighbour's numbers as its own.
    """
    escaped = repo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return ResourceLease.key.like(f"{escaped}:%", escape="\\")


#: Postgres' unique-violation SQLSTATE. `_take` must distinguish "somebody else
#: got this key" from any other integrity failure: catching every IntegrityError
#: as a lost race turned a genuine schema or constraint fault into a silent retry
#: and then a misleading "contended" 409, hiding the real error (round 1's F24).
_UNIQUE_VIOLATION = "23505"


def _is_lost_race(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == _UNIQUE_VIOLATION


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
    except IntegrityError as e:
        # Somebody committed between our sweep and our insert. Their claim is
        # the real one; roll ours back and let the caller report the holder.
        await session.rollback()
        # ...but ONLY for the unique violation this races on. Any other
        # integrity failure is a real fault, and swallowing it as "contended"
        # would retry it eight times and then report a queue that is not there.
        if not _is_lost_race(e):
            raise
        return None
    await session.refresh(claim)
    return claim


def _renew_onto(claim: ResourceLease, *, holder: str, ttl: int,
                sess: str | None, note: str | None, now: datetime) -> None:
    """Apply a renew to a claim, in the one place every renew path uses.

    ``renewed: true`` used to mean two different things. ``POST /claim``'s
    ordinary branch extended the TTL and updated note and session; its
    race-loser branch returned the winner untouched and uncommitted (F05), and
    the allocator's session short-circuit did the same (F21). A caller retrying a
    long allocation was told it was renewed and then had its claim lapse anyway.
    One helper, so the word cannot drift again.
    """
    claim.holder = holder
    claim.ttl_seconds = ttl
    claim.expires_at = now + timedelta(seconds=ttl)
    if sess:
        claim.session = sess
    if note:
        claim.note = note


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
    #: Required in practice for a release claim that named one — see
    #: :func:`_may_mutate`. Ownership of a release number is the session's, not
    #: the box's, because on this fleet two agents per box are two branches.
    session: str | None = None


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
    if body.kind in RESERVED_KINDS:
        raise HTTPException(409, detail={
            "error": f"{body.kind!r} claims are allocated, not taken",
            "kind": body.kind,
            "hint": "use POST /release/claim — see RESERVED_KINDS for why"})

    now = _utcnow()
    sess = _clean(body.session)
    await _sweep_lapsed(session, body.kind, body.key, now)
    await session.commit()

    held = await _held(session, body.kind, body.key, now)
    if held is not None:
        if not _may_mutate(held, holder, sess):
            raise _conflict(body.kind, body.key, held)
        _renew_onto(held, holder=holder, ttl=body.ttl, sess=sess,
                    note=body.note, now=now)
        await session.commit()
        return {**_view(held), "claimed": True, "renewed": True}

    claim = await _take(session, kind=body.kind, key=body.key, holder=holder,
                        ttl=body.ttl, sess=sess, note=body.note, now=now)
    if claim is None:
        # Lost the insert race. Re-read rather than reporting a generic failure:
        # the loser of a race is exactly the caller who most needs to know who won.
        winner = await _held(session, body.kind, body.key, now)
        if winner is None:
            raise HTTPException(409, detail={
                "error": "claim contended; try again",
                "kind": body.kind, "key": body.key})
        if _may_mutate(winner, holder, sess):
            # A real renew, written and committed — not a `renewed: true` over an
            # untouched row. Same request, same reported outcome, same effect,
            # whether or not a race happened to occur (F05).
            _renew_onto(winner, holder=holder, ttl=body.ttl, sess=sess,
                        note=body.note, now=now)
            await session.commit()
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
    if not _may_mutate(claim, holder, body.session):
        raise _not_yours(claim)
    now = _utcnow()
    kind, key = claim.kind, claim.key
    # Conditional UPDATE, not read-then-write. The checks below were being made
    # against a row that a concurrent sweep could release between the read and
    # the write, so a lapsed claim another agent had already taken could still be
    # "renewed" and reported `claimed: true` (round 1's F04). The predicate is
    # the same one `_held` uses, evaluated by the database at write time.
    #
    # This is the PR's own thesis applied to the paths it had not been applied
    # to: the INSERT was made atomic by the unique index while every UPDATE still
    # looked first and hoped.
    done = await session.execute(
        update(ResourceLease)
        .where(ResourceLease.id == body.claim_id,
               ResourceLease.released_at.is_(None),
               ResourceLease.expires_at > now)
        .values(expires_at=now + timedelta(seconds=claim.ttl_seconds))
        .returning(ResourceLease.id)
    )
    if done.scalar_one_or_none() is None:
        await session.rollback()
        # Deliberately NOT auto-renewed. Lapsing means another agent may already
        # have taken this key, and silently extending would hand one resource to
        # two holders — the whole failure being fixed.
        raise HTTPException(409, detail={
            "error": "claim is no longer held; re-take via POST /claim",
            "kind": kind, "key": key})
    await session.commit()
    fresh = await session.get(ResourceLease, body.claim_id)
    return _view(fresh)


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
    if not _may_mutate(claim, holder, body.session):
        raise _not_yours(claim)
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
        .where(ResourceLease.kind == "release", _repo_prefix(repo))
    )
    seen = [v for v in (parse_version(k[len(prefix):]) for k in rows) if v]
    return max(seen) if seen else None


async def _my_live_release(session: AsyncSession, repo: str, holder: str,
                           sess: str | None, now: datetime) -> ResourceLease | None:
    """This caller's own live release claim for a repo, or None.

    Scoped by session AND machine. Keying it on the session alone was round 1's
    F03: session ids are the board's public addressing scheme — peers quote them
    at each other constantly — so any agent that knew or reused another's session
    string was handed back that agent's live claim, holder and note included, as
    if it were its own.
    """
    if not sess:
        return None
    mine = await session.scalar(
        select(ResourceLease)
        .where(ResourceLease.kind == "release", _repo_prefix(repo),
               ResourceLease.session == sess,
               ResourceLease.released_at.is_(None),
               ResourceLease.expires_at > now)
        .order_by(ResourceLease.acquired_at.desc())
        .limit(1)
    )
    return mine if mine is not None and same_machine(mine.holder, holder) else None


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
    # Scoped to the session AND the machine — see `_my_live_release` for why the
    # session alone was a hole rather than a shortcut.
    sess = _clean(body.session)
    mine = await _my_live_release(session, body.repo, holder, sess, now)
    if mine is not None:
        got = parse_version(mine.key[len(body.repo) + 1:])
        # ...but only while it still satisfies what the caller asked for. A
        # caller renumbering off a collision re-runs this with a HIGHER `after`,
        # and handing back the very number it is trying to escape reports success
        # for the one thing it asked not to happen (round 1's F20). Below the
        # floor, fall through and allocate.
        if got is not None and (told is None or got > told):
            _renew_onto(mine, holder=holder, ttl=body.ttl, sess=sess,
                        note=body.note, now=now)
            await session.commit()
            return {**_view(mine), "version": fmt_version(got),
                    "claimed": True, "renewed": True,
                    "after_unreadable": unreadable}

    for _attempt in range(8):
        # Re-checked every pass, not once before the loop. Two concurrent
        # requests carrying one session could both pass a pre-loop check (neither
        # had committed yet), and the insert loser then allocated the NEXT number
        # instead of finding its twin — one session holding two numbers, which is
        # exactly what the idempotency was built to prevent (round 1's F06).
        if _attempt:
            mine = await _my_live_release(session, body.repo, holder, sess, now)
            if mine is not None:
                got = parse_version(mine.key[len(body.repo) + 1:])
                if got is not None and (told is None or got > told):
                    return {**_view(mine), "version": fmt_version(got),
                            "claimed": True, "renewed": True,
                            "after_unreadable": unreadable}
        known = await _highest_known(session, body.repo)
        floor = max([v for v in (told, known) if v is not None], default=(0, 0))
        candidate = _next_version(floor)
        if candidate is None:
            raise HTTPException(409, detail={
                "error": "release namespace exhausted", "repo": body.repo,
                "floor": fmt_version(floor)})
        key = release_key(body.repo, candidate)

        await _sweep_lapsed(session, "release", key, now)
        await session.commit()
        held = await _held(session, "release", key, now)
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
            if sess and held.session and sess == held.session \
                    and same_machine(held.holder, holder):
                _renew_onto(held, holder=holder, ttl=body.ttl, sess=sess,
                            note=body.note, now=now)
                await session.commit()
                return {**_view(held), "version": fmt_version(candidate),
                        "claimed": True, "renewed": True,
                        "after_unreadable": unreadable}
            # Held, so it is not free however the arithmetic came out.
            # `_highest_known` will now see it and the next pass moves on.
            continue

        note = body.note or (f"held for {body.branch}" if body.branch else None)
        claim = await _take(session, kind="release", key=key, holder=holder,
                            ttl=body.ttl, sess=sess, note=note, now=now)
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


class ReleaseReclaimIn(BaseModel):
    """Swap one release number for another, atomically."""

    repo: str = Field(min_length=1, max_length=256)
    #: The claim being given up. Named by id rather than by version so a caller
    #: cannot accidentally release a number it never held.
    claim_id: uuid.UUID
    #: Carried onto the new claim, as `POST /release/claim` already does. Its
    #: absence meant a renumber could not say which branch the new number was
    #: for, so `GET /releases` lost the "what is landing soon" answer at exactly
    #: the moment the branch changed number (round 1's F12).
    branch: str | None = None
    after: str | None = None
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    session: str | None = None
    note: str | None = Field(default=None, max_length=500)


@router.post("/release/reclaim")
async def reclaim_release(
    body: ReleaseReclaimIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Give up a number and take the next free one, as ONE step.

    **The renumber is the dangerous moment, not the first pick, and the evidence
    is that both of 2026-08-16's collisions were renumbers.** Choosing a version
    at the start feels like a decision, so it gets announced and re-read;
    replacing one feels like bookkeeping, so it gets neither. Both branches that
    collided that morning were renumbering off an earlier collision.

    Doing it as release-then-claim through the two endpoints above reopens
    exactly the race this table closes: between the release and the claim the
    caller holds nothing, and the window is widest precisely when the namespace
    is contended — which is the only time anyone renumbers. So it is one call and
    one transaction.

    **The old claim is given up only if a new one was taken.** A failed
    allocation leaves the caller holding what it had, because the alternative is
    an agent with a CHANGELOG full of a number it no longer owns and nothing to
    replace it with. That asymmetry is the whole reason this is not two calls.
    """
    now = now_pre = _utcnow()
    old = await session.get(ResourceLease, body.claim_id)
    if old is None:
        raise HTTPException(404, "claim not found")
    if not _may_mutate(old, holder, body.session):
        raise _not_yours(old)
    # Liveness, which `renew_claim` checked and this did not (round 1's F07).
    # Without it a timed-out retry, a concurrent reclaim of the same id, or
    # simply an already-released claim all passed every other check and minted
    # ANOTHER number — the double allocation the session idempotency exists to
    # prevent, with the renumber path having no equivalent guard. It also stopped
    # `giving_up.released_at = now` from overwriting a release that had already
    # happened, rewriting history to say it was let go later than it was.
    if old.released_at is not None or old.expires_at <= now_pre:
        raise HTTPException(409, detail={
            "error": "that claim is no longer held; take a fresh one via POST /release/claim",
            "key": old.key,
            "released": old.released_at.isoformat() if old.released_at else None,
            "lapsed": old.lapsed})
    if old.kind != "release":
        raise HTTPException(409, detail={
            "error": "not a release claim", "kind": old.kind, "key": old.key})

    prefix = f"{body.repo}:"
    if not old.key.startswith(prefix):
        raise HTTPException(409, detail={
            "error": "that claim belongs to another repo",
            "key": old.key, "repo": body.repo})

    told = parse_version(body.after)
    unreadable = body.after is not None and told is None
    # Read off the row ONCE, before any commit or rollback. Both expire every
    # attribute on the session's objects, and an expired attribute read back
    # under async SQLAlchemy is a lazy load outside the greenlet — a
    # `MissingGreenlet` at the exact moment this endpoint is doing its job,
    # since the retry path is reached only when the namespace is contended.
    # Found by the concurrent test below and by nothing else.
    old_id, old_key = old.id, old.key
    old_session, old_note = old.session, old.note
    gave_up = parse_version(old_key[len(prefix):])

    for _attempt in range(8):
        known = await _highest_known(session, body.repo)
        floor = max([v for v in (told, known) if v is not None], default=(0, 0))
        candidate = _next_version(floor)
        if candidate is None:
            raise HTTPException(409, detail={
                "error": "release namespace exhausted", "repo": body.repo,
                "still_holding": fmt_version(gave_up) if gave_up else None})
        key = release_key(body.repo, candidate)

        await _sweep_lapsed(session, "release", key, now)
        await session.commit()
        if await _held(session, "release", key, now) is not None:
            continue

        # Both writes in one transaction: the old row is released in the same
        # commit that takes the new one, so there is no instant at which this
        # caller holds neither. If the INSERT loses its race the rollback takes
        # the release with it, which is what makes the failure path safe rather
        # than merely unlikely.
        #
        # `old` is re-fetched each pass because the commit above expired it.
        # `old` is re-fetched BEFORE `fresh` is added to the session. Loading a
        # row whose attributes a commit expired can trigger autoflush, and with
        # the INSERT already pending that flush emits it outside this
        # try/except — so a lost race would surface as an unhandled
        # IntegrityError instead of a retry (round 1's F28).
        giving_up = await session.get(ResourceLease, old_id)
        if giving_up is None or giving_up.released_at is not None:
            raise HTTPException(409, detail={
                "error": "that claim was released while renumbering; nothing was taken",
                "key": old_key})
        note = body.note or (f"held for {body.branch}" if body.branch else old_note)
        fresh = ResourceLease(kind="release", key=key, holder=holder,
                              session=_clean(body.session) or old_session,
                              note=note,
                              ttl_seconds=body.ttl,
                              expires_at=now + timedelta(seconds=body.ttl))
        session.add(fresh)
        giving_up.released_at = now
        try:
            await session.commit()
        except IntegrityError as e:
            await session.rollback()
            if not _is_lost_race(e):
                raise
            continue
        await session.refresh(fresh)
        return {**_view(fresh), "version": fmt_version(candidate),
                "claimed": True, "renewed": False,
                # Named back so the caller can check the swap it just made
                # against the number it has already written into eight files.
                "gave_up": fmt_version(gave_up) if gave_up else None,
                "after_unreadable": unreadable}

    raise HTTPException(409, detail={
        "error": "could not reclaim: the namespace is contended",
        "repo": body.repo,
        "still_holding": fmt_version(gave_up) if gave_up else None,
        "advice": "you still hold your original number; retry"})


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
        .where(ResourceLease.kind == "release", _repo_prefix(repo))
        .order_by(ResourceLease.acquired_at.desc())
        .limit(limit)
    ))
    out = []
    for c in rows:
        v = parse_version(c.key[len(prefix):])
        out.append({
            # The id every mutating endpoint requires. Without it a client that
            # discovered its claim here had to go to `GET /claims` to act on it
            # (round 1's F31).
            "claim_id": str(c.id),
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
