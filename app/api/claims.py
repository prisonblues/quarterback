"""Atomic claims on named resources: who is doing this, and who is landing it.

#99 wanted one thing: "somebody is landing on ``main`` right now." It is a claim
on a small shared namespace where a collision is silent until it is expensive,
and this repo made the case the hard way — two agents merging within a minute of
each other, three claiming overlapping work inside 56 seconds.

**Announcing is not claiming, and that is the finding this module exists for.**
Two agents once announced the same version one second apart and were both correct
from what they could see. Announcement does not force the next agent to look;
asking for the resource does, because the answer can be no.

**The key is derived, never composed** — see :mod:`app.claimkey`. The whole of
#172's evidence is what happens without that rule: the plan wrote
``kind='work', key='<repo>#163'``, an agent wrote ``kind='issue'`` with the same
string, and the two subsystems reported different answers about the same issue in
the same second. Every path here canonicalises through one function, so a caller
that composes a pair by hand still writes the row every reader looks for.

**Advisory, not a lock, and it must never be described otherwise.** The board
cannot gate github.com: a human merging in the UI, or an agent not enrolled here,
lands regardless. What this removes is collisions between agents that ask, which
is the observed failure mode and the entire claim. The correctness backstop stays
where it was — the pre-land verdict re-checked after base movement (#96), and CI
on ``main``. If a skill ever calls this "the merge lock", the skill is wrong.

**There is no release allocator here any more (#172).** ``kind='release'`` shipped
in #46 to stop two branches picking the same version. What actually stopped it was
taking the number from the CHANGELOG at the ref being merged into — nine releases
landed that way in a day with no collisions, while the allocator's own rows went
stale for every PR still open. #122 finished the argument: the number is applied
on ``main`` after the merge, by ``scripts/release.py``, so no branch names one and
there is no race left to allocate against. A namespace nobody claims in does not
need an allocator, and a stale record of it is worse than none: it is the second
spelling this module is now built to prevent.

The endpoints went; the KIND could still be written, because canonicalisation
passes an unrecognised kind through and the ``RESERVED_KINDS`` guard was deleted
with the allocator. So ``release`` is now a *retired* kind rather than an unknown
one (:data:`app.claimkey.RETIRED_KINDS`) and ``POST /claim`` refuses it with 422,
naming ``release.py``. A deletion that leaves one path able to write the
rows is not a deletion.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import identify, reader
from app.claimkey import (
    REF_KINDS,
    WORK_KINDS,
    BadRef,
    board_object,
    canonical,
    canonical_kind,
    canonical_repo,
    derive,
    repo_of,
)
from app.db import async_session, get_session
from app.identity import address_clause, is_human, resolve_alias, same_machine
from app.models.plan import Plan
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease

router = APIRouter(tags=["claim"])

#: The plan item a claim writes is best-effort, so its failures have to land
#: somewhere a person can find them — the caller is told, and told once.
_log = logging.getLogger("app.claim")

#: Default hold, in seconds. A land takes minutes and a unit of work is held from
#: "I have picked this up" to "the PR merged", which on this repo has run to
#: hours. Long enough not to lapse mid-work, short enough that a crashed holder
#: frees it within one coffee. (The release number used to be the long case; the
#: allocator that held it is deleted, and the land is the remaining reason.)
DEFAULT_TTL = 3600
MAX_TTL = 86_400

#: The longest session identifier that means anything. A session id is a uuid or
#: a short handle; unbounded free text on an identifier is a column of somebody
#: else's paste, and every free-text field on this table is bounded but this one.
MAX_SESSION = 200


def _utcnow() -> datetime:
    return datetime.now(UTC)


def clean_session(s: str | None) -> str | None:
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


def may_mutate(claim: ResourceLease, holder: str, session_id: str | None) -> bool:
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
        return clean_session(session_id) == claim.session
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


def claim_view(c: ResourceLease) -> dict:
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
    different facts: the first is work that finished, the second is a session
    that stopped answering, and a dashboard that showed them the same way would
    report an abandoned land as a completed one. ``qbdata`` filters on this column
    to tell them apart. (The allocator that first needed the distinction is
    deleted — the distinction outlived it, which is why it is still here.)
    """
    await session.execute(
        update(ResourceLease)
        .where(ResourceLease.kind == kind, ResourceLease.key == key,
               ResourceLease.released_at.is_(None), ResourceLease.expires_at <= now)
        .values(released_at=now, lapsed=True)
    )


async def live_claim(session: AsyncSession, kind: str, key: str,
                     now: datetime | None = None) -> ResourceLease | None:
    """The claim actually holding a key right now, or None. Also for other routers.

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
               ResourceLease.expires_at > (now or _utcnow()))
        .limit(1)
    )


#: Postgres' unique-violation SQLSTATE. `_take` must distinguish "somebody else
#: got this key" from any other integrity failure: catching every IntegrityError
#: as a lost race turned a genuine schema or constraint fault into a silent retry
#: and then a misleading "contended" 409, hiding the real error (round 1's F24).
_UNIQUE_VIOLATION = "23505"


def is_unique_violation(exc: IntegrityError) -> bool:
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
        if not is_unique_violation(e):
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


class ResourceRef(BaseModel):
    """WHICH resource, so the board can work out the key (#172).

    The preferred way to claim. ``kind`` names a resource this board understands
    (:data:`app.claimkey.REF_KINDS`) and the key is derived from it, so two agents
    describing one collision cannot produce two keys — which is what happened for
    four months and is why ``claims()`` and ``plan_read`` disagreed about the same
    issue in the same second.
    """

    kind: str = Field(min_length=1, max_length=32,
                      description=f"one of: {', '.join(REF_KINDS)}")
    #: ``owner/name``, and only for the kinds that need one. A plan or an item is
    #: identified by its board id, so sending a repo alongside would invite two
    #: keys for one row.
    repo: str | None = Field(default=None, max_length=256)
    #: The issue number, PR number, branch name or board id. Free-form because
    #: what it must be depends on ``kind``, and :mod:`app.claimkey` is the one
    #: place that decides.
    value: str = Field(min_length=1, max_length=512)


class ClaimIn(BaseModel):
    """Either a ``ref`` (derived — preferred) or a ``kind``/``key`` pair.

    The composed pair is still accepted, and canonicalised through the same
    function the derived path uses. Refusing it outright would have been the
    tidier API and the wrong move: every agent, dashboard and skill on the fleet
    composes one today, and an endpoint that 422s them all writes no claims at
    all — which is the state #172 was filed about.
    """

    ref: ResourceRef | None = None
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    key: str | None = Field(default=None, min_length=1, max_length=512)
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    #: What you are doing with it. Optional, and worth sending: it is what the
    #: next agent is shown instead of a bare refusal — and, absent `title`, what
    #: names the plan item this claim writes.
    note: str | None = Field(default=None, max_length=500)
    #: A better handle for the plan item, when the caller has one. The server is
    #: forge-free on purpose (#327) so it cannot read an issue's real title; a
    #: client that just ran `gh issue view` can, and passing it here is the whole
    #: of the "clients enrich" half of #427. Ignored when the key names no unit of
    #: work, and ignored on a renew. Bounded to the plan's own title length —
    #: `_pickup_title` truncates too, so this only stops a megabyte arriving.
    title: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _one_way_or_the_other(self) -> ClaimIn:
        if self.ref is None and not (self.kind and self.key):
            raise ValueError(
                "send `ref` (preferred: {kind, repo, value} — the board derives the "
                "key) or both `kind` and `key`")
        if self.ref is not None and (self.kind or self.key):
            # Not merged, and not silently preferring one: a request carrying both
            # is a caller with two ideas about what it is claiming, and guessing
            # which one it meant is how a claim lands on the wrong resource.
            raise ValueError("send `ref` OR `kind`/`key`, not both")
        return self

    def resolve(self) -> tuple[str, str]:
        """The canonical ``(kind, key)`` this request is for. Raises :class:`BadRef`."""
        if self.ref is not None:
            return derive(self.ref.kind, repo=self.ref.repo, value=self.ref.value)
        return canonical(self.kind or "", self.key or "")


class ClaimRefIn(BaseModel):
    claim_id: uuid.UUID
    #: Required in practice for ANY claim that named one — see
    #: :func:`may_mutate`. Ownership is the session's, not the box's, because on
    #: this fleet two agents per box are two agents; the release-only reading of
    #: this line was the #142 bug, and a comment that outlives its rule is how it
    #: got there.
    session: str | None = Field(default=None, max_length=MAX_SESSION)


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """One claim, as asked for — :func:`acquire`'s argument.

    An options object rather than eight keyword parameters, so a caller reads as
    one intent and a new field (``session_owned``) costs no call site anything.
    ``sess`` is normalised on the way in, because "" and None are the same
    absence and only one of them is storable (see :func:`clean_session`).
    """

    kind: str
    key: str
    holder: str
    ttl: int = DEFAULT_TTL
    sess: str | None = None
    note: str | None = None
    now: datetime = field(default_factory=_utcnow)
    #: Is this claim owned by the SESSION rather than by the box? Off by default
    #: — a land claim belongs to the machine, so an agent that restarts can pick
    #: its own back up. The plan (v2.39) turns it on: a machine runs several
    #: agents at once, all authenticating as that one token, and "two agents on
    #: one box both hold item #60" is the exact duplicated work it exists to
    #: prevent. Opt-in and per-request, so it changes nothing else.
    session_owned: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "sess", clean_session(self.sess))
        # Canonicalised HERE rather than in the endpoint, so that every caller of
        # `acquire` gets it — the plan router, the endpoint, and the fourth and
        # fifth caller this dataclass exists to make cheap. One gate is the whole
        # lesson of #172: the defect was two subsystems agreeing by convention,
        # and a normalisation each of them has to remember to call is the same
        # convention with more steps.
        kind, key = canonical(self.kind, self.key)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "key", key)


def _may_renew(claim: ResourceLease, req: ClaimRequest) -> bool:
    """May this request renew the claim it found, instead of being refused?

    :func:`may_mutate` is the table-wide rule and stays exactly as it is — a
    separate branch is changing it for release claims and this must not collide
    with that. What is added here is the caller's own answer to "what is a
    worker": with ``session_owned`` a claim taken by session A is not session B's
    to renew even on the same machine, and a claim that recorded no session at
    all still falls back to the box, because there is nothing finer to compare.
    """
    if not may_mutate(claim, req.holder, req.sess):
        return False
    if req.session_owned and claim.session:
        return req.sess == claim.session
    return True


async def _sweep_apart(kind: str, key: str, now: datetime) -> None:
    """Retire this key's lapsed claims in a session of the caller's own.

    :func:`acquire` used to sweep on the caller's session and commit — which
    committed whatever else that unit of work had pending, before doing anything
    the caller asked for. Nothing broke, because both callers happened to be
    clean at that line; the function is extracted precisely so a fourth and fifth
    caller will exist, and "it commits your half-finished mutation first" is not
    a precondition a signature can carry. So the housekeeping runs on its own
    connection and commits only itself.
    """
    async with async_session() as own:
        await _sweep_lapsed(own, kind, key, now)
        await own.commit()


def _refuse_pending_work(session: AsyncSession) -> None:
    """:func:`acquire` commits, so it must not be handed uncommitted work."""
    pending = bool(session.new or session.deleted) or any(
        session.is_modified(o, include_collections=False) for o in session.dirty)
    if pending:
        raise RuntimeError(
            "acquire() commits: flush and commit your own writes before calling it, "
            "or they land as part of the claim — and roll back with it.")


async def acquire(session: AsyncSession, req: ClaimRequest) -> tuple[ResourceLease, bool]:
    """Take or renew a claim on ``(kind, key)``. Raises :func:`_conflict` if held.

    The whole of ``POST /claim``'s body, as a function, so a feature that needs
    an atomic claim reuses this one rather than growing a second implementation
    of "who has this right now" — which is the outcome #99 was filed to avoid and
    the plan (v2.39) is the third feature to want. Returns ``(claim, renewed)``.

    Re-claiming something your own machine already holds is a RENEW, not a
    conflict — the same rule ``POST /lease`` applies, and for the same reason: a
    claim belongs to the box, and an agent that restarts mid-land must be able to
    pick its own claim back up rather than be locked out by its former self.
    Callers for whom the *session* is the worker say so with
    ``session_owned=True`` (see :func:`_may_renew`).

    **It commits.** It has to: atomicity here is a committed INSERT against a
    unique index, not a lock. So it is not composable inside a larger unit of
    work, and rather than leave that as a docstring nobody reads at the call
    site, a session with pending work is refused outright.
    """
    _refuse_pending_work(session)
    await _sweep_apart(req.kind, req.key, req.now)

    held = await live_claim(session, req.kind, req.key, req.now)
    if held is not None:
        if not _may_renew(held, req):
            raise _conflict(req.kind, req.key, held)
        _renew_onto(held, holder=req.holder, ttl=req.ttl, sess=req.sess,
                    note=req.note, now=req.now)
        await session.commit()
        return held, True

    claim = await _take(session, kind=req.kind, key=req.key, holder=req.holder,
                        ttl=req.ttl, sess=req.sess, note=req.note, now=req.now)
    if claim is not None:
        return claim, False

    # Lost the insert race. Re-read rather than reporting a generic failure:
    # the loser of a race is exactly the caller who most needs to know who won.
    winner = await live_claim(session, req.kind, req.key, req.now)
    if winner is None:
        raise HTTPException(409, detail={
            "error": "claim contended; try again", "kind": req.kind, "key": req.key})
    if not _may_renew(winner, req):
        raise _conflict(req.kind, req.key, winner)
    # A real renew, written and committed — not a `renewed: true` over an
    # untouched row. Same request, same reported outcome, same effect,
    # whether or not a race happened to occur (F05).
    _renew_onto(winner, holder=req.holder, ttl=req.ttl, sess=req.sess,
                note=req.note, now=req.now)
    await session.commit()
    return winner, True


@router.post("/claim")
async def take_claim(
    body: ClaimIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Claim a named resource, and put it on the plan.

    Send ``ref`` and the board derives the key. Send ``kind``/``key`` and it is
    canonicalised onto the same value — and the response says so, because an agent
    that believes it holds ``issue/<repo>#163`` while the row reads
    ``work/<repo>#163`` is the #172 defect with the parties swapped.

    **A fresh claim on an issue or a PR also writes the plan item for it, at the
    top of that repo's list** (#427). Picking work up is the one act that says
    what the fleet is actually doing, and until this it was the only one that left
    the plan unchanged — #426 was filed and claimed within eleven seconds and the
    PLANS panel showed nothing, because the claim/item join only ran item-first.
    The new row is returned as ``plan_item`` so the caller need not go and look.

    It is best-effort by design and it is the *claim* that is guaranteed. The item
    is a second commit — :func:`acquire` commits, so it cannot be one — and if
    that commit fails the claim still stands and ``plan_item`` comes back null
    with ``plan_item_error`` saying why. A claim that landed with no item costs a
    row on a dashboard; a claim refused because a plan write failed costs the
    duplicated work the claim exists to prevent, and they are not close.

    **A renew repairs.** Because the write can fail and the claim survives it
    anyway, something has to be able to put it right afterwards, or "best-effort"
    means "permanently invisible on the first bad day" — the exact state this
    feature was built to abolish. So a renew runs the same write, which finds the
    item already there and returns it, and writes it when it is not.
    """
    try:
        kind, key = body.resolve()
    except BadRef as e:
        raise HTTPException(422, str(e)) from None

    claim, renewed = await acquire(session, ClaimRequest(
        kind=kind, key=key, holder=holder, ttl=body.ttl,
        sess=body.session, note=body.note, now=_utcnow()))
    out = {**claim_view(claim), "claimed": True, "renewed": renewed}
    if body.ref is None and (body.kind, body.key) != (kind, key):
        out["derived_from"] = {"kind": body.kind, "key": body.key}
        out["note_on_key"] = (
            f"you asked for {body.kind}/{body.key!r}; the board keys that resource "
            f"as {kind}/{key!r}. Send `ref` instead and you never have to know.")
    # On a renew too, and that is a REPAIR rather than a second write. Gating this
    # on a fresh take left the one state this feature exists to abolish with no way
    # out of it: if the plan write failed once — a transient database fault, a
    # deploy mid-migration — the claim was invisible on the plan for as long as it
    # kept being renewed, because every renew skipped the only code that could have
    # fixed it. `item_for_claim` is idempotent by construction (an existing open
    # item is returned, not rewritten), so the repair costs one indexed SELECT on a
    # renew that had nothing to fix.
    out.update(await _plan_item_for(
        session, kind=kind, key=key, holder=holder,
        note=body.note, title=body.title))
    return out


async def _plan_item_for(session: AsyncSession, **kw: object) -> dict:
    """:func:`item_for_claim`, with its failure kept away from the claim.

    Imported here rather than at module scope because ``app.api.plan`` imports
    this module — it reuses :func:`acquire` so that "who has this right now" has
    one implementation (#99) — and the plan is the natural home for a function
    that writes a plan item. One deferred import is the cheaper of the two ways
    out; the other is a third module holding the rank helpers, which would move
    ``_lock_scope`` and ``_scope_items`` away from the only other things that use
    them.
    """
    from app.api.plan import item_for_claim  # noqa: PLC0415 — see docstring

    try:
        item = await item_for_claim(session, **kw)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001 — the claim is what must survive
        # Rolled back so the caller's session is usable: this runs after
        # `acquire` has committed, so nothing of the CLAIM is at risk here.
        await session.rollback()
        _log.warning("claim %s/%s: plan item not written: %s", kw.get("kind"),
                     kw.get("key"), e, exc_info=True)
        return {"plan_item": None, "plan_item_error": str(e)}
    if item is None:
        # Not a failure: `work_ref` declined the key, because a merge claim, a
        # board object or a row in somebody's database is not a unit of work the
        # plan can hold. Silent, because saying so on every such claim would be
        # noise on the many paths this is true of.
        return {"plan_item": None}
    return {"plan_item": {"item_id": str(item.id), "rank": item.rank,
                          "rank_source": item.rank_source, "title": item.title,
                          "repo": item.repo}}


@router.post("/claim/renew")
async def renew_claim(
    body: ClaimRefIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    claim = await session.get(ResourceLease, body.claim_id)
    if claim is None:
        raise HTTPException(404, "claim not found")
    if not may_mutate(claim, holder, body.session):
        raise _not_yours(claim)
    now = _utcnow()
    kind, key = claim.kind, claim.key
    # Conditional UPDATE, not read-then-write. The checks below were being made
    # against a row that a concurrent sweep could release between the read and
    # the write, so a lapsed claim another agent had already taken could still be
    # "renewed" and reported `claimed: true` (round 1's F04). The predicate is
    # the same one `live_claim` uses, evaluated by the database at write time.
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
    return claim_view(fresh)


@router.post("/claim/release")
async def release_claim(
    body: ClaimRefIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Let go. Idempotent, and never deletes: the row is the history, so a
    released claim stays on the table as a record that this agent held this
    resource between these two times."""
    claim = await session.get(ResourceLease, body.claim_id)
    if claim is None:
        raise HTTPException(404, "claim not found")
    if not may_mutate(claim, holder, body.session):
        raise _not_yours(claim)
    if claim.released_at is None:
        claim.released_at = _utcnow()
        await session.commit()
    return {"claim_id": str(claim.id), "released": True, "lapsed": claim.lapsed}


async def release_session_claims(
    session: AsyncSession, *, sess_key: str, holder: str, now: datetime
) -> tuple[list[dict], list[dict]]:
    """Let go of every live claim a session took. ``(released, refused)`` (#277).

    The stop half of a session's lifecycle. Until this existed there was no way
    to give a claim back except one call per claim id, by a conversation that
    still remembered taking it — so the only thing that ever actually freed a
    dead agent's work was the TTL, and #263 is what that costs: a seat whose
    context was reset kept every claim the previous conversation took, renewing
    them from a conversation with no memory of the work, where passive expiry
    could never reach them because nothing had died.

    **Owned by the session, exactly as a mutation is.** The filter is the
    ownership rule this module already states — :func:`may_mutate`, applied as a
    filter rather than restated — so ending a session cannot touch a co-tenant's
    claims on the same box. Applied in Python over the rows rather than compiled
    into SQL for the same reason ``held_claims`` gives about its own filter:
    three re-derivations of one rule is three chances to disagree with it.

    **A claim that named NO session is left alone**, and that is not an
    oversight. Such a claim belongs to the machine (``may_mutate``'s fallback):
    ``create-worktree`` takes one before the agent that will use the tree
    exists, and sweeping it up when some unrelated session on the box ended
    would free a checkout's issue out from under whoever is about to pick it up.

    ``lapsed`` stays false. The holder let go; nobody vanished. That is the
    distinction ``_sweep_lapsed`` exists to keep, and an ended session is on the
    "finished" side of it however unhappily it ended — something reported this,
    which is the whole difference from a TTL running out.

    **"Every live claim" means every one live when this ran.** A claim committed
    by that session after this SELECT survives, and there is no lock that would
    change it: the session being ended is a process that has stopped or is about
    to, so a claim arriving afterwards is a race inside a dying agent rather than
    a gap here. The TTL is still underneath it, as it is for every other claim.

    **A PERSON ending a session releases everything stamped with it** (#378).
    :func:`may_mutate` asks which machine the caller is, and a person is not one:
    ``human/rich`` shares a machine with nothing on the fleet, so the ordinary
    rule refuses every row and a browser's end of a stuck session would report
    ``released_claims: []`` beside ``refused_claims: [everything]`` — the verb
    doing none of its job while returning 200. Their authority is the session
    instead, which is the stronger half of the agent's own rule anyway: the
    SELECT above admits only claims stamped with the key being ended, so a claim
    naming no session is still left to its machine. The credential is the one
    that reorders the plan — a person proved at the edge, and no bearer token
    can authenticate into that namespace (see :func:`app.auth.human`).
    """
    rows = list(await session.scalars(
        select(ResourceLease).where(
            ResourceLease.session == sess_key,
            ResourceLease.released_at.is_(None),
            ResourceLease.expires_at > now,
        ).order_by(ResourceLease.acquired_at.desc())
    ))
    released, refused = [], []
    by_person = is_human(holder)
    for claim in rows:
        if not (by_person or may_mutate(claim, holder, sess_key)):
            refused.append(claim_view(claim))
            continue
        claim.released_at = now
        released.append(claim_view(claim))
    return released, refused


@router.get("/claims")
async def list_claims(
    kind: str | None = None,
    key: str | None = None,
    ref_kind: str | None = Query(default=None,
                                 description=f"derive the key instead: {', '.join(REF_KINDS)}"),
    ref_value: str | None = Query(default=None, description="issue/PR number, branch, or id"),
    repo: str | None = Query(default=None, description="`owner/name`, for a ref that needs one"),
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
    asked = {"kind": kind, "key": key}
    # Derived here rather than by the caller, for the reason the write path is:
    # the MCP layer is a separate package with no import of this one, so a client
    # that composed the lookup key would be a SECOND implementation of the rule —
    # which is the defect #172 is about, moved into the read path.
    if ref_kind or ref_value:
        if not (ref_kind and ref_value):
            raise HTTPException(422, "ref_kind and ref_value go together: which "
                                     "issue, PR, branch or id?")
        if kind or key:
            # Refused, not silently preferred — the same rule `ClaimIn` applies to
            # the write, and for the same reason. That validator's argument is
            # that "a request carrying both is a caller with two ideas about what
            # it is claiming"; a *read* carrying both is a caller with two ideas
            # about what it is asking, and answering about one of them without
            # saying which is how a lookup reports "nobody holds that" about a
            # row that is right there. One rule, both directions.
            raise HTTPException(422, "ask by `ref_kind`/`ref_value` (the board "
                                     "derives the key) OR by `kind`/`key`, not both")
        try:
            kind, key = derive(ref_kind, repo=repo, value=ref_value)
        except BadRef as e:
            raise HTTPException(422, str(e)) from None
    elif kind and key:
        # Canonicalised on the way in, exactly as the write path is. A caller
        # looking up `kind=issue&key=<repo>#163` is asking about a resource, not
        # about a string, and answering "no claims" about a row that is right
        # there is how #172's plan-versus-claims disagreement read from outside.
        try:
            kind, key = canonical(kind, key)
        except BadRef as e:
            raise HTTPException(422, str(e)) from None
    elif kind:
        # A kind with no key. This branch did not exist, so `?kind=issue` filtered
        # on the literal string `issue` and matched nothing — every claim on a
        # unit of work is stored under `work` now, and `kind` alone is what the
        # pre-#172 vocabulary trained every agent, skill and dashboard to send.
        # An empty answer about held resources is the defect this module's own
        # docstring names, so the alias is folded here and the fold is REPORTED:
        # `pr` and `issue` share one kind by design, so this filter is coarser
        # than the caller asked for and saying nothing about that would trade one
        # silent wrong answer for another.
        try:
            kind = canonical_kind(kind)
        except BadRef as e:
            raise HTTPException(422, str(e)) from None
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
    out: dict = {"claims": [
        {**claim_view(c),
         "released": c.released_at.isoformat() if c.released_at else None,
         "lapsed": c.lapsed}
        for c in rows]}
    if (asked["kind"], asked["key"]) not in ((None, None), (kind, key)):
        # Said out loud, exactly as `POST /claim` says it: a caller that believes
        # it asked about `issue/<repo>#163` while the filter read `work/…` is the
        # #172 defect with the parties swapped.
        out["filtered_on"] = {"kind": kind, "key": key}
        out["asked_for"] = asked
        note = (f"you asked about {asked['kind']}/{asked['key'] or '(any key)'}; the "
                f"board keys that as {kind}/{key or '(any key)'}")
        if asked["key"] is None:
            note += (" — a kind alone can no longer tell an issue from a PR, because "
                     "the key's shape carries that now. Send `ref_kind`/`ref_value` "
                     "for one resource, or `kind` and `key` together.")
        out["note_on_kind"] = note
    return out


@router.get("/claims/in-flight")
async def in_flight_claims(
    repo: str = Query(description="`owner/name` — the repository to count"),
    _: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """How much work is in flight in this repository, right now, fleet-wide (#337).

    The number nothing in the fleet has ever known. Eight agents were run against
    one ``main`` on 2026-08-22; two branches independently minted migration
    ``0029``, a third was renumbered twice mid-flight, and the largest open diff
    went ``DIRTY`` the moment the first of them landed. Every mechanism that
    looked like it might govern that — the merge claim, the merge queue,
    ``app/ordering.py``, the review queue — sits *downstream* of the decision to
    start, and orders an exit that has already been built.

    **Claims are the count, and the reason is jurisdictional.** Not worktrees:
    ``git worktree list`` on this box returned 48, mostly debris from finished
    work, and a count that conflates dead trees with live work bounds the wrong
    thing. Not open PRs: by then the branch exists. A claim is what the board has
    authority over — an unlanded unit of work inside this system is represented
    by one, and one outside it was never told to us and is not ours to police.
    A claim also self-heals, which is the property a worktree count could not
    have: a TTL frees a dead holder's slot with nobody intervening (#135), where
    a worktree reaper would have to tell a stalled agent from a slow one.

    **What counts is a unit of work in a REPOSITORY.** Three things are live
    ``work`` claims and are deliberately not units of work:

    * ``plan:<uuid>`` / ``item:<uuid>`` — board objects. :func:`board_object`
      names them and they are skipped; a claim on a plan is a claim on the
      ordering, and the planner holding it is not a ninth agent in flight.
    * ``plan-order:<repo>`` — #232's ordering claim. :func:`repo_of` answers None
      for it, so it drops out without a special case, which is the point of
      asking the key rather than matching on strings.
    * ``kind=merge`` — the last few seconds of a branch's life (#99), and by
      definition work that is already built. Excluded by the kind filter, since
      :data:`WORK_KINDS` is what this counts.

    So the answer is: live claims naming an issue or a PR in this repo, whoever
    holds them. **Whoever** matters — the bound is per repository and the fleet
    is several machines against one board, so a per-holder count would admit
    ``max`` agents *per box*. It is also why a human starting a ninth thing by
    hand is absorbed rather than exempt: they run ``create-worktree`` and take
    the same claim an agent does, and the count neither knows nor cares which it
    was.

    **A count, not a list a caller re-derives one from** — the argument
    :func:`held_claims` makes about itself, applied to the second gate. The repo
    attribution is :func:`app.claimkey.repo_of`, read off the key; a client that
    filtered ``GET /claims`` by key shape would be a fourth implementation of the
    join #172 is about, and enforcement built on a re-derivation is enforcement
    that can disagree with the board about what is held.

    Nothing here decides anything. The ceiling lives in the repo's policy file
    and the refusal happens at the checkout (``create-worktree``); this endpoint
    only answers how many, so that a caller with no bound configured never asks.
    """
    try:
        repo = canonical_repo(repo)
    except BadRef as e:
        raise HTTPException(422, str(e)) from None
    now = _utcnow()
    # Narrowed in SQL to the kinds that can FOLD onto `work` — not to `work`
    # alone. Every write canonicalises (see `ClaimRequest`), but rows written
    # before #172 did not, and a claim stored as `kind='issue'` is the same claim
    # under the same unique index. Counting the canonical spelling only would
    # under-report the window and admit an agent into a slot that is taken.
    rows = list(await session.scalars(
        select(ResourceLease).where(
            ResourceLease.kind.in_(tuple(WORK_KINDS)),
            ResourceLease.released_at.is_(None),
            ResourceLease.expires_at > now,
        ).order_by(ResourceLease.acquired_at.desc())
    ))
    claims = [claim_view(c) for c in rows
              if board_object(c.kind, c.key) is None and repo_of(c.kind, c.key) == repo]
    return {
        "repo": repo,
        # The one field a gate reads.
        "count": len(claims),
        "claims": claims,
        "holders": sorted({c["holder"] for c in claims}),
        "checked": now.isoformat(),
    }


async def _board_scopes(
    session: AsyncSession, rows: list[ResourceLease]
) -> dict[tuple[str, str], str]:
    """The repo each ``plan:``/``item:`` claim among ``rows`` is against (#172).

    ``repo_of`` answers None for a board object and is right to: the key is an id,
    and an id says nothing about a repository. But the *row* does — a plan carries
    its scope and an item carries its own — so a claim on the plan for
    ``prisonblues/quarterback`` **is** a claim in that repo, and reporting it as
    ``unattributed`` made ``held`` false for an agent holding the plan for the
    very repo it was asking about. Which is the one thing this endpoint exists to
    get right: #172's whole design routes the fuzzy case through a plan claim, so
    a gate blind to plan claims is blind to the intake the issue added.

    A NULL scope stays unattributed, because a fleet-scoped plan genuinely does
    not say which repo — that is the open question #172 closes on, and this
    answers it the way the schema does rather than guessing from the items.

    One statement per table, keyed on the ids actually present, so the endpoint
    stays two queries whatever the row count.
    """
    wanted: set[tuple[str, str]] = set()
    for c in rows:
        obj = board_object(c.kind, c.key)
        if obj is not None:
            wanted.add(obj)
    if not wanted:
        return {}
    out: dict[tuple[str, str], str] = {}
    for prefix, model in (("plan", Plan), ("item", PlanItem)):
        ids = [i for kind, i in wanted if kind == prefix]
        if not ids:
            continue
        for row_id, scope in await session.execute(
            select(model.id, model.repo).where(model.id.in_(ids))
        ):
            if not scope:
                continue
            try:
                # `_norm_scope` lower-cases on the way in, so this normally
                # changes nothing; it runs anyway because a row written before it
                # did must not silently compare unequal to the `repo` query
                # parameter, which IS canonicalised.
                out[(prefix, str(row_id))] = canonical_repo(scope)
            except BadRef:
                # A scope that is not a repo shape this board keys. Left out, so
                # the claim reads as unattributed rather than attributed to a
                # repo no caller can name in a query.
                continue
    return out


@router.get("/claim/held")
async def held_claims(
    repo: str | None = Query(default=None, description="`owner/name`; omit for every repo"),
    holder_q: str | None = Query(default=None, alias="holder",
                                 description="whose claims; defaults to yours"),
    session_q: str | None = Query(
        default=None, alias="session",
        description="narrow to one session's claims — plus any claim that named "
                    "no session, which belongs to the machine"),
    caller: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Does this agent hold a live claim in this repository — yes or no (#172).

    The question a pickup gate has to ask, and the reason it is a separate
    endpoint rather than a filter on ``GET /claims``: enforcement must be a
    **deterministic boolean**, not a list a caller re-derives the answer from.
    Three callers re-deriving it is three chances to get the repo attribution
    wrong, and the fleet has already spent an evening on exactly that — the
    dashboards' own ``issue_claims`` joins on the key shape while the plan filters
    on ``kind``, and the two disagreed.

    The repo attribution is :func:`app.claimkey.repo_of`, so it is read off the
    key rather than from a column a caller fills in — the same rule that makes the
    key itself derived. A plan or item key names a board object rather than a
    repo, so the join is finished against the row (:func:`_board_scopes`); a key
    that still names no repo after that is reported under ``unattributed`` rather
    than dropped, because "I am holding something, and it does not say which
    repo" is a different answer from "I am holding nothing", and a gate that
    collapsed them would stop an agent that is demonstrably working.

    **Whose claims, and the rule is this module's own.** This asked for
    ``holder == whose`` — plain equality, and the only ownership test on this
    table that did. Every other one goes through :func:`same_machine`
    (:func:`may_mutate`, ``_may_renew``, the plan router's ``_is_mine``), because
    the name half of an identity is *board-allocated per* ``X-Agent-Key`` and is
    recycled. The two clients that make up this feature do not send the same
    headers: the MCP server sends ``X-Agent-Key``, so an agent claiming through
    the ``claim`` tool writes under ``zeus/amber-otter``, while the harness CLIs
    send only ``Authorization``, so ``qb-claim`` — and therefore
    ``create-worktree`` — writes under the bare ``zeus``. Under plain equality
    each was invisible to the other: the pickup gate reported ``held: false`` for
    an agent that had just claimed through the tool, and the tool reported
    ``held: false`` for the claim the checkout took on its behalf. The suite could
    not see it because ``tests/conftest.py`` sends no key, so writer and reader
    were always the same bare string.

    So the holder filter is :func:`app.identity.address_clause`, the same
    machine-scoped, alias-aware clause ``GET /active`` already uses on
    ``Lease.holder``: it matches the machine root, this agent's name and its
    permanent key form — and *not* a co-tenant's name. An agent that comes back
    under a different name therefore still sees everything the machine holds and
    everything its key answers to; a claim written under a name it has since lost
    is the one case the widening does not recover, and it is the same gap
    :func:`app.identity.resolve_alias` documents for a retired agent's mail.

    **And the session is what separates co-tenants**, exactly as it does for a
    mutation: a claim that named a session belongs to that session, and a claim
    that named none falls back to the machine because there is nothing finer to
    compare. ``may_mutate``'s rule, read as a filter. That is what lets the
    checkout claim work: ``create-worktree`` records no session — the agent that
    will use the tree does not exist yet — so the claim belongs to the box until
    somebody picks it up, and the session that picks it up can see it.
    """
    if repo is not None:
        try:
            repo = canonical_repo(repo)
        except BadRef as e:
            raise HTTPException(422, str(e)) from None
    now = _utcnow()
    # Defaults to the caller. An agent asking "am I holding anything" must not
    # have to name itself — that is the client-supplied-identity mistake
    # `identify` exists to avoid, and it is how a co-tenant's claim would come
    # back as your own.
    whose, aliases = await resolve_alias(session, holder_q or caller)
    stmt = select(ResourceLease).where(
        address_clause(ResourceLease.holder, whose, aliases),
        ResourceLease.released_at.is_(None),
        ResourceLease.expires_at > now,
    )
    wanted_session = clean_session(session_q)
    if wanted_session:
        stmt = stmt.where(or_(ResourceLease.session == wanted_session,
                             ResourceLease.session.is_(None)))
    rows = list(await session.scalars(stmt.order_by(ResourceLease.acquired_at.desc())))
    scopes = await _board_scopes(session, rows)

    in_repo, unattributed = [], []
    for c in rows:
        where = repo_of(c.kind, c.key)
        if where is None:
            obj = board_object(c.kind, c.key)
            where = scopes.get(obj) if obj is not None else None
        if where is None:
            unattributed.append(claim_view(c))
        elif repo is None or where == repo:
            in_repo.append({**claim_view(c), "repo": where})
    return {
        "repo": repo,
        "holder": whose,
        "session": wanted_session,
        # The one field a gate reads. `held` is true when this agent holds
        # something attributable to the repo asked about; with no repo, when it
        # holds anything attributable at all.
        "held": bool(in_repo),
        "claims": in_repo,
        "unattributed": unattributed,
        "checked": now.isoformat(),
    }
