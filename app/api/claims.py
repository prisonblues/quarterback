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

**A unique index is only unique within a spelling (v2.38).** The key was built
from a repo string the caller supplied as free text, and this fleet supplies two
of them for one repo — the origin remote's basename and GitHub's
``nameWithOwner`` — so the table kept two independent sequences over one repo and
handed 2.36 to two agents 28 minutes apart with ``claimed: true`` on both. That
is the tenth collision and the first this allocator produced, which is worse than
the announcement it replaced: an announcement leaves the caller uncertain. Every
repo string now goes through :mod:`app.repokey` first. See there for the
canonical form and for why a bare basename is a lookup rather than a namespace.

The two kinds ship off one table on purpose. Two independent implementations of
"who has this right now" is the outcome #99 was filed to avoid.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.db import get_session
from app.identity import same_machine
from app.models.resource_lease import ResourceLease
from app.models.review import ReviewRun
from app.repokey import (
    canonical_repo,
    like_escape,
    like_prefix,
    name_half,
    repo_basename,
    resolve_against,
    split_repo_head,
    version_tail,
)

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

    So the rule now follows the kind rather than the table. The machine is
    necessary throughout; for a release claim that named a session, that session
    is necessary too. A release claim with no session falls back to the machine,
    because there is nothing finer to check and refusing outright would strand
    claims taken by callers that sent none.
    """
    if not same_machine(claim.holder, holder):
        return False
    if claim.kind == "release" and claim.session:
        return _clean(session_id) == claim.session
    return True


def _not_yours(claim: ResourceLease) -> HTTPException:
    return HTTPException(403, detail={
        "error": "not your claim",
        "kind": claim.kind, "key": claim.key,
        "held_by": claim.holder, "session": claim.session,
        "hint": ("a release claim is owned by the session that took it, not by the "
                 "machine: two agents on one box are two branches"),
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


def _claimed(c: ResourceLease, as_given: str, *, renewed: bool) -> dict:
    """A claim response, saying so when the key it landed on is not the one sent.

    Normalising silently would leave a caller holding a string the board does not
    use, and the next thing it does with that string is usually to look the claim
    up again. Told, not advised: the rewrite still happens either way.
    """
    out = {**_view(c), "claimed": True, "renewed": renewed}
    if as_given != c.key:
        out["key_as_given"] = as_given
    return out


def _allocated(c: ResourceLease, version: tuple[int, int], repo: str, as_given: str,
               *, renewed: bool, unreadable: bool) -> dict:
    """An allocation response, naming the repo the number was actually taken under.

    ``repo`` is echoed because the caller may not have sent this spelling, and a
    number is only meaningful with the namespace it came from — that being the
    whole of #148. ``repo_as_given`` appears only when the two differ, so a caller
    can notice its own spelling drifting rather than discover it at the collision.
    """
    out = {**_view(c), "version": fmt_version(version), "repo": repo,
           "claimed": True, "renewed": renewed, "after_unreadable": unreadable}
    if as_given != repo:
        out["repo_as_given"] = as_given
    return out


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
    """A LIKE clause matching one repo's release keys — canonical AND legacy.

    Two prefixes, not one, and the second is the reason this fix has a floor it
    can trust. Rows written before v2.38 are keyed on whichever spelling the
    caller happened to use, and migration 0020 rewrites the ones it can resolve —
    but a basename it could not expand stays where it is, and a number that has
    fallen out of the floor is a number this board will hand out twice. So the
    read side also looks in the bare-basename bucket that the write side can no
    longer reach.

    **This is deliberately the unsafe-looking direction, because it is the safe
    one.** ``prisonblues/quarterback`` and ``someone-else/quarterback`` both read
    the legacy ``quarterback:`` rows, so one repo's history could raise the
    other's floor — and that costs a skipped number, while the alternative costs
    a rename across eight files. Nothing new is ever written there, so the bucket
    only shrinks.

    ``startswith`` is not used: it compiles to ``LIKE 'prefix%'`` and does NOT
    escape ``_`` or ``%``, both of which are LIKE wildcards and both of which
    occur in real repo names. ``acme/my_repo`` matched ``acme/myXrepo`` (v2.33's
    F19) — so one repo's allocation floor could be raised by another's, and
    `/releases` could list a neighbour's numbers as its own.
    """
    clauses = [ResourceLease.key.like(like_prefix(repo), escape="\\")]
    legacy = name_half(repo)
    if legacy != repo:
        clauses.append(ResourceLease.key.like(like_prefix(legacy), escape="\\"))
    return or_(*clauses)


async def _repos_named(session: AsyncSession, base: str) -> list[str]:
    """Every canonical repo this board has seen whose name half is ``base``.

    The expansion table for ``qb-hook``'s spelling, drawn from what the board
    already holds rather than from a list somebody has to maintain: review runs
    record ``nameWithOwner`` by documented contract, and any claim taken under a
    full spelling names one too. Case-insensitive on the way in, because the
    stored form is GitHub's and the lookup key is already folded.
    """
    esc = like_escape(base)
    runs = await session.scalars(
        select(ReviewRun.repo).where(ReviewRun.repo.ilike(f"%/{esc}", escape="\\")).distinct()
    )
    keys = await session.scalars(
        select(ResourceLease.key)
        .where(or_(ResourceLease.key.ilike(f"%/{esc}:%", escape="\\"),
                   ResourceLease.key.ilike(f"%/{esc}#%", escape="\\")))
        .distinct()
    )
    found = {canonical_repo(r) for r in runs}
    found |= {canonical_repo(split_repo_head(k)[0]) for k in keys}
    return sorted(r for r in found if r is not None and name_half(r) == base)


async def _resolve_repo(session: AsyncSession, given: str) -> tuple[str | None, list[str]]:
    """``(canonical repo, candidates)``, expanding a bare basename if it is unambiguous."""
    if canonical_repo(given) is None and (base := repo_basename(given)) is not None:
        return resolve_against(given, set(await _repos_named(session, base)))
    return resolve_against(given, set())


def _unknown_repo(given: str, candidates: list[str]) -> HTTPException:
    """400 naming the form that works, and the choices when there were several.

    A refusal rather than a guess, because coining a namespace is the failure
    being fixed and it fails silently — with ``claimed: true`` on it. The
    candidates are listed when there are any: an ambiguous basename is a question
    only the caller can answer, and answering it for them would pick an owner.
    """
    return HTTPException(400, detail={
        "error": f"cannot tell which repo {given!r} means",
        "repo": given,
        "expected": "owner/name (GitHub's nameWithOwner), a full remote URL, or a "
                    "basename this board has already seen under exactly one owner",
        "candidates": candidates,
        "hint": ("several owners answer to that name — say which"
                 if candidates else
                 "this board has no repo by that name; send owner/name"),
    })


async def _require_repo(session: AsyncSession, given: str) -> str:
    repo, candidates = await _resolve_repo(session, given)
    if repo is None:
        raise _unknown_repo(given, candidates)
    return repo


async def _canonical_key(session: AsyncSession, key: str) -> str:
    """A generic claim key with its repo head canonicalised, or unchanged.

    **Never refuses, unlike the release path, and the asymmetry is deliberate.**
    ``ReleaseClaimIn.repo`` is a typed field documented as a repo, so a string
    that is not one is a caller error worth reporting. A generic ``key`` is the
    caller's own vocabulary by design — ``kind='deploy', key='portainer-stack-189'``
    is a perfectly good claim with no repo in it — so a head that does not resolve
    is left exactly as sent, and only a head this board can positively identify as
    a repo is rewritten.
    """
    head, rest = split_repo_head(key)
    repo, _ = await _resolve_repo(session, head)
    return f"{repo}{rest}" if repo is not None else key


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

    **The key's repo head is canonicalised first**, so ``quarterback#142`` and
    ``prisonblues/quarterback#142`` are one claim rather than two agents each
    holding "the same" issue (#148). A key with no repo in it is untouched — see
    :func:`_canonical_key`.
    """
    if body.kind in RESERVED_KINDS:
        raise HTTPException(409, detail={
            "error": f"{body.kind!r} claims are allocated, not taken",
            "kind": body.kind,
            "hint": "use POST /release/claim — see RESERVED_KINDS for why"})

    now = _utcnow()
    sess = _clean(body.session)
    key = await _canonical_key(session, body.key)
    await _sweep_lapsed(session, body.kind, key, now)
    await session.commit()

    held = await _held(session, body.kind, key, now)
    if held is not None:
        if not _may_mutate(held, holder, sess):
            raise _conflict(body.kind, key, held)
        _renew_onto(held, holder=holder, ttl=body.ttl, sess=sess,
                    note=body.note, now=now)
        await session.commit()
        return _claimed(held, body.key, renewed=True)

    claim = await _take(session, kind=body.kind, key=key, holder=holder,
                        ttl=body.ttl, sess=sess, note=body.note, now=now)
    if claim is None:
        # Lost the insert race. Re-read rather than reporting a generic failure:
        # the loser of a race is exactly the caller who most needs to know who won.
        winner = await _held(session, body.kind, key, now)
        if winner is None:
            raise HTTPException(409, detail={
                "error": "claim contended; try again",
                "kind": body.kind, "key": key})
        if _may_mutate(winner, holder, sess):
            # A real renew, written and committed — not a `renewed: true` over an
            # untouched row. Same request, same reported outcome, same effect,
            # whether or not a race happened to occur (F05).
            _renew_onto(winner, holder=holder, ttl=body.ttl, sess=sess,
                        note=body.note, now=now)
            await session.commit()
            return _claimed(winner, body.key, renewed=True)
        raise _conflict(body.kind, key, winner)
    return _claimed(claim, body.key, renewed=False)


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
        # Through the same canonicaliser the write path uses, or a lookup by the
        # spelling you claimed with would miss the row you just took.
        stmt = stmt.where(ResourceLease.key == await _canonical_key(session, key))
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

    ``repo`` must already be canonical — see :func:`_repo_prefix`, which also
    explains why this scan reaches into the pre-v2.38 bare-basename bucket. The
    version is read off the END of the key rather than by removing a ``repo:``
    prefix, because a legacy row is not keyed on the spelling this call was made
    with, and a mis-sliced key parses as nothing and vanishes from the floor.
    """
    rows = await session.scalars(
        select(ResourceLease.key)
        .where(ResourceLease.kind == "release", _repo_prefix(repo))
    )
    seen = [v for v in (parse_version(version_tail(k)) for k in rows) if v]
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

    **``repo`` is canonicalised before anything is read or written** (#148/#150).
    It used to be the string the caller sent, and this fleet sends two of them
    for one repo, so the board kept two floors and issued 2.36 twice with
    ``claimed: true`` on both. An allocator with two namespaces is not an
    allocator; it is an announcement that sounds authoritative.
    """
    now = _utcnow()
    repo = await _require_repo(session, body.repo)
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
    mine = await _my_live_release(session, repo, holder, sess, now)
    if mine is not None:
        got = parse_version(version_tail(mine.key))
        # ...but only while it still satisfies what the caller asked for. A
        # caller renumbering off a collision re-runs this with a HIGHER `after`,
        # and handing back the very number it is trying to escape reports success
        # for the one thing it asked not to happen (round 1's F20). Below the
        # floor, fall through and allocate.
        if got is not None and (told is None or got > told):
            _renew_onto(mine, holder=holder, ttl=body.ttl, sess=sess,
                        note=body.note, now=now)
            await session.commit()
            return _allocated(mine, got, repo, body.repo,
                              renewed=True, unreadable=unreadable)

    for _attempt in range(8):
        # Re-checked every pass, not once before the loop. Two concurrent
        # requests carrying one session could both pass a pre-loop check (neither
        # had committed yet), and the insert loser then allocated the NEXT number
        # instead of finding its twin — one session holding two numbers, which is
        # exactly what the idempotency was built to prevent (round 1's F06).
        if _attempt:
            mine = await _my_live_release(session, repo, holder, sess, now)
            if mine is not None:
                got = parse_version(version_tail(mine.key))
                if got is not None and (told is None or got > told):
                    return _allocated(mine, got, repo, body.repo,
                                      renewed=True, unreadable=unreadable)
        known = await _highest_known(session, repo)
        floor = max([v for v in (told, known) if v is not None], default=(0, 0))
        candidate = _next_version(floor)
        if candidate is None:
            raise HTTPException(409, detail={
                "error": "release namespace exhausted", "repo": repo,
                "floor": fmt_version(floor)})
        key = release_key(repo, candidate)

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
                return _allocated(held, candidate, repo, body.repo,
                                  renewed=True, unreadable=unreadable)
            # Held, so it is not free however the arithmetic came out.
            # `_highest_known` will now see it and the next pass moves on.
            continue

        note = body.note or (f"held for {body.branch}" if body.branch else None)
        claim = await _take(session, kind="release", key=key, holder=holder,
                            ttl=body.ttl, sess=sess, note=note, now=now)
        if claim is None:
            continue
        # `after_unreadable` is said rather than swallowed: an `after` this
        # board could not parse means the allocation rested on board history
        # alone, and a caller that mistyped its own version wants to know that
        # before it writes the number into eight files.
        return _allocated(claim, candidate, repo, body.repo,
                          renewed=False, unreadable=unreadable)

    raise HTTPException(409, detail={
        "error": "could not allocate a release number: the namespace is contended",
        "repo": repo,
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
    repo = await _require_repo(session, body.repo)
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

    # Compared through the canonicaliser rather than by prefix, because the claim
    # being given up may predate v2.38 and be keyed on the other spelling of this
    # very repo — and a renumber refused as "another repo's claim" would strand
    # exactly the rows #148 is about, at exactly the moment their holder is trying
    # to get off a collision.
    old_repo, _ = await _resolve_repo(session, split_repo_head(old.key)[0])
    if old_repo != repo:
        raise HTTPException(409, detail={
            "error": "that claim belongs to another repo",
            "key": old.key, "repo": repo})

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
    gave_up = parse_version(version_tail(old_key))

    for _attempt in range(8):
        known = await _highest_known(session, repo)
        floor = max([v for v in (told, known) if v is not None], default=(0, 0))
        candidate = _next_version(floor)
        if candidate is None:
            raise HTTPException(409, detail={
                "error": "release namespace exhausted", "repo": repo,
                "still_holding": fmt_version(gave_up) if gave_up else None})
        key = release_key(repo, candidate)

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
        return {**_allocated(fresh, candidate, repo, body.repo,
                             renewed=False, unreadable=unreadable),
                # Named back so the caller can check the swap it just made
                # against the number it has already written into eight files.
                "gave_up": fmt_version(gave_up) if gave_up else None}

    raise HTTPException(409, detail={
        "error": "could not reclaim: the namespace is contended",
        "repo": repo,
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

    **Through the same canonicaliser the allocator uses** (#148). This is how the
    bug was found — a caller read the numbers back under one spelling and did not
    see one it knew was held — so a read that could still disagree with the write
    would leave the detection half as broken as the allocation half was.
    """
    now = _utcnow()
    given, repo = repo, await _require_repo(session, repo)
    rows = list(await session.scalars(
        select(ResourceLease)
        .where(ResourceLease.kind == "release", _repo_prefix(repo))
        .order_by(ResourceLease.acquired_at.desc())
        .limit(limit)
    ))
    out = []
    for c in rows:
        v = parse_version(version_tail(c.key))
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
            # The stored key, spelling included. A row rewritten by 0020 or left
            # under a basename it could not expand is visible here rather than
            # inferred from the repo above, which is the only way a reader can
            # tell a legacy row from a current one.
            "key": c.key,
        })
    highest = await _highest_known(session, repo)
    body = {"repo": repo, "releases": out,
            # What the NEXT call would allocate, absent a higher `after` from the
            # caller. Advisory and racy by nature — reading it is not claiming it,
            # which is the distinction this whole module is about.
            "highest_known": fmt_version(highest) if highest else None}
    if given != repo:
        body["repo_as_given"] = given
    return body
