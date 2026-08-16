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
from dataclasses import dataclass
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
    identified_repo,
    known_repos_from,
    like_prefix,
    lookup_name,
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


@dataclass(frozen=True)
class _Repo:
    """The namespace a release call resolved to, and the spelling the caller sent.

    One value rather than two positional arguments threaded through every
    allocation path: they are never meaningful apart, and passing them
    separately pushed :func:`_allocated` to seven parameters against this
    project's limit of five.
    """

    canonical: str
    as_given: str


def _allocated(c: ResourceLease, version: tuple[int, int], repo: _Repo, *,
               renewed: bool, unreadable: bool) -> dict:
    """An allocation response, naming the repo the number was actually taken under.

    The repo is echoed because the caller may not have sent this spelling, and a
    number is only meaningful with the namespace it came from — that being the
    whole of #148. ``repo_as_given`` appears only when the two differ, so a caller
    can notice its own spelling drifting rather than discover it at the collision.
    """
    out = {**_view(c), "version": fmt_version(version), "repo": repo.canonical,
           "claimed": True, "renewed": renewed, "after_unreadable": unreadable}
    if repo.as_given != repo.canonical:
        out["repo_as_given"] = repo.as_given
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


async def _first_held(session: AsyncSession, kind: str, keys: list[str],
                      now: datetime) -> ResourceLease | None:
    """The claim holding the first of ``keys`` that anybody holds, or None.

    **Two spellings of one key can both go live, and that is this release's own
    bug arriving through the door it deliberately left open.** A generic key
    whose head the board cannot identify is stored exactly as sent, which is
    right (see :func:`_canonical_key`). But the board's knowledge grows: the
    repo's first release claim or first review run makes that basename
    resolvable, and from then on the same request canonicalises. Checking only
    the canonical key at that point would never see the bare row still sitting
    there unreleased, so ``quarterback#142`` and ``prisonblues/quarterback#142``
    would be held simultaneously by two agents each told they had it.

    So the un-canonicalised spelling is checked too — the claim path's equivalent
    of what :func:`_repo_prefix` does for the release floor. The canonical key
    goes first because it is the one anything new is written under; the bare one
    is a bucket that can only shrink.

    **What this does NOT give you, said plainly: cross-spelling exclusivity is a
    read, not the index.** The unique index covers one ``(kind, key)``, and these
    are two, so a check-then-insert across them can lose a race the way every
    pre-v2.31 claim did. The window is narrow and worth stating exactly, because
    "it is racy" and "it is racy in this one situation" are different facts: both
    requests read the same committed state, so they reach the same verdict about
    whether the repo is identified, and they can only disagree if a review run or
    a first release claim commits *between* them. Nothing else can move a repo
    from unknown to known. Where that does happen the loser is a duplicate claim
    of the kind migration 0020 exists to clean up, not a duplicated release
    number — the allocator writes canonical keys only and its floor reads both
    buckets. Closing it properly needs an index this table does not have; taking
    the claim and then undoing it would add a rollback path to the hottest
    endpoint here for a window measured in one request pair.
    """
    for key in keys:
        found = await _held(session, kind, key, now)
        if found is not None:
            return found
    return None


def _repo_prefix(repo: str, *, legacy: bool):
    """A LIKE clause matching one repo's release keys, optionally the legacy ones too.

    The legacy half is the reason this fix has a floor it can trust. Rows written
    before v2.38 are keyed on whichever spelling the caller happened to use, and
    migration 0020 rewrites the ones it can resolve — but a basename it could not
    expand stays where it is, and a number that has fallen out of the floor is a
    number this board will hand out twice. So the read side also looks in the
    bare-basename bucket that the write side can no longer reach.

    **``legacy`` is per call site, because the union is only safe in one
    direction.** ``prisonblues/quarterback`` and ``someone-else/quarterback`` both
    read the legacy ``quarterback:`` rows, so one repo's history can raise the
    other's floor — which costs a skipped number, against a rename across eight
    files. That trade is right for :func:`_highest_known`, whose whole job is to
    be conservative about what may already have been handed out. It is wrong for
    anything that reports *ownership*: an unresolved ``quarterback:2.9`` could
    belong to either owner, so including it let one owner renew and be handed
    back the other's live claim as its own idempotent allocation, and made
    ``GET /releases`` list a neighbour's numbers under every basename-sharing
    repo. Those call sites pass ``legacy=False``.

    The honest consequence: ``highest_known`` can exceed the newest row
    ``/releases`` lists, because the floor sees the legacy bucket and the row list
    does not. That is the correct pair of answers — the number really may be
    taken, and it really is not provably this repo's.

    ``startswith`` is not used: it compiles to ``LIKE 'prefix%'`` and does NOT
    escape ``_`` or ``%``, both of which are LIKE wildcards and both of which
    occur in real repo names. ``acme/my_repo`` matched ``acme/myXrepo`` (v2.33's
    F19) — so one repo's allocation floor could be raised by another's, and
    `/releases` could list a neighbour's numbers as its own.

    The legacy clause is ``ilike``, not ``like``. Its pattern is a folded
    basename while the rows it is looking for are pre-v2.38 keys written in
    whatever case the caller's remote had — ``qb-hook`` takes the remote basename
    verbatim, and repo names are commonly mixed case. A case-sensitive ``LIKE``
    therefore could not see ``Quarterback:2.36`` at all: it is exactly the row
    0020 leaves alone, it drops out of the floor, and the allocator re-issues the
    number. The canonical clause stays ``like``, because every canonical key is
    lowered by construction.
    """
    base = name_half(repo)
    if base == repo:
        # Not a canonical repo at all but a stranded basename, which only
        # :func:`_reclaim_namespace` produces. Its whole bucket is pre-v2.38 keys
        # in whatever case the caller's remote had, so the case-insensitive match
        # is the only one that finds them.
        return ResourceLease.key.ilike(like_prefix(repo), escape="\\")
    canonical = ResourceLease.key.like(like_prefix(repo), escape="\\")
    if not legacy:
        return canonical
    return or_(canonical, ResourceLease.key.ilike(like_prefix(base), escape="\\"))


async def _repos_named(session: AsyncSession, base: str) -> set[str]:
    """Every canonical repo this board has seen whose name half is ``base``.

    The expansion table for ``qb-hook``'s spelling, drawn from what the board
    already holds rather than from a list somebody has to maintain.

    **Only from sources a generic claim cannot forge**, which is the correction
    this table needed. It used to scan every ``resource_leases`` key regardless of
    kind, so a perfectly legal ``kind='deploy', key='attacker/thing#1'`` minted
    the repo identity ``attacker/thing`` — and a later release request for the
    bare basename ``thing`` was routed to it, or refused as ambiguous, which is a
    denial of service on somebody else's basename. The two sources left are
    written by machinery rather than by asking: ``review_runs.repo`` comes from
    the review pipeline and is ``nameWithOwner`` by documented contract, and a
    ``kind='release'`` key can only be produced by the allocator, since
    ``POST /claim`` refuses that kind outright (:data:`RESERVED_KINDS`). A repo
    nobody has ever reviewed or released simply has to be named in full the first
    time, which always works.

    One statement, and the filtering is done here rather than in SQL. A
    ``'%/name'`` pattern cannot use an ordinary index, so it was already a scan;
    matching in Python costs the same scan and drops two bugs with it — a
    ``review_runs`` row holding a full remote URL or a trailing slash ends with
    neither ``/name`` nor ``/name:``, and a full generic key with no separator at
    all was invisible to the pattern while the migration counted it. The residual
    cost is a ``DISTINCT`` over ``review_runs.repo`` and over the release keys per
    unresolved basename; release keys are one row per number ever allocated and
    distinct review repos are a handful, so the scanned set is bounded by the
    board's repo count rather than by its history.
    """
    seen = await session.scalars(
        select(ReviewRun.repo.label("repo_or_key"))
        .union(select(ResourceLease.key.label("repo_or_key"))
               .where(ResourceLease.kind == "release"))
    )
    return {r for r in known_repos_from(seen) if name_half(r) == base}


async def _resolve_repo(session: AsyncSession, given: str) -> tuple[str | None, list[str]]:
    """``(canonical repo, candidates)``, expanding a bare basename if it is unambiguous.

    The basename is derived once, here, and doubles as the test for whether the
    lookup is needed at all: :func:`repo_basename` returns None for a canonical
    string, and :func:`resolve_against` never consults ``known`` for one.
    """
    base = repo_basename(given)
    known = await _repos_named(session, base) if base is not None else set()
    return resolve_against(given, known)


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


async def _canonical_key(session: AsyncSession, key: str) -> tuple[str, str | None]:
    """``(canonicalised key, the repo it names)`` — the repo being None when there is none.

    The repo is returned as well as applied because the caller needs to know
    *whether* a head was identified, not only what it turned into: an unchanged
    key means "nothing here is a repo" for ``portainer-stack-189`` and "this
    already was the canonical spelling" for ``acme/thing#1``, and only the second
    of those has an older spelling worth looking for. See :func:`_key_spellings`.

    **Never refuses, unlike the release path, and the asymmetry is deliberate.**
    ``ReleaseClaimIn.repo`` is a typed field documented as a repo, so a string
    that is not one is a caller error worth reporting. A generic ``key`` is the
    caller's own vocabulary by design — ``kind='deploy', key='portainer-stack-189'``
    is a perfectly good claim with no repo in it — so a head that does not resolve
    is left exactly as sent.

    **Identified, not merely parsed.** Rewriting any two-segment head broke the
    promise above in a way nobody would notice: ``Prod/Blue:resource`` came back
    as ``prod/blue:resource``, and two genuinely distinct caller resources
    differing only in case became one claim. So the head is rewritten only when
    its canonical form is a repo this board has actually seen — see
    :func:`app.repokey.identified_repo`, which states what that costs.

    Shares :func:`app.repokey.identified_repo` with migration 0020, so the key an
    endpoint writes and the key the migration rewrites cannot drift apart.
    """
    head, rest = split_repo_head(key)
    base = lookup_name(head)
    if base is None:
        return key, None
    repo = identified_repo(head, await _repos_named(session, base))
    return (f"{repo}{rest}", repo) if repo is not None else (key, None)


def _key_spellings(key: str, repo: str | None, as_given: str) -> list[str]:
    """Every key this one resource can be sitting under, canonical first.

    **A generic key can be live under two spellings at once, and that is this
    release's own bug arriving through the door it deliberately left open.** A
    head the board cannot identify is stored exactly as sent, which is right.
    But the board's knowledge grows: the repo's first release claim or review run
    makes that basename resolvable, and from then on the same request
    canonicalises. Looking only at the canonical key at that point never sees the
    bare row still sitting there unreleased, so ``quarterback#142`` and
    ``prisonblues/quarterback#142`` are both held and two agents are each told
    they have #142.

    So the bare-basename spelling is looked for too — the claim path's equivalent
    of the legacy clause in :func:`_repo_prefix`, with the same trade and the same
    justification: a basename is ambiguous across owners, so this can occasionally
    report a same-named other repo's claim, and being told who holds something
    costs a conversation while two holders cost a rename. The bucket only shrinks,
    because nothing is written there once the repo is identified.

    It is added ONLY when a repo was identified. Splitting a name half off an
    unidentified head would invent a lookup: ``Prod/Blue:resource`` is not also
    ``Blue:resource``, and treating it as such would refuse an unrelated claim.
    """
    keys = [key]
    if repo is not None:
        legacy = f"{name_half(repo)}{split_repo_head(key)[1]}"
        if legacy != key:
            keys.append(legacy)
    if as_given not in keys:
        keys.append(as_given)
    return keys


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


def _conflict(kind: str, held: ResourceLease, as_given: str) -> HTTPException:
    """409 naming WHO holds it and WHY, and the key as the caller spelled it.

    The refusal is the coordination, not the denial: an agent told only "held"
    can do nothing but retry, and one told "held by zeus/thorn-spruce, landing
    #128, expires 12:04" can go and talk to them or pick up something else.

    ``key_as_given`` is on the error and not only on the success, because the
    error is the response a caller actually reads. Told ``acme/nswork#142`` is
    held when it asked for ``nswork#142``, a caller has no way to connect the two
    strings — and "somebody else holds a key I have never heard of" is a worse
    message than the bare refusal it replaced.
    """
    detail = {
        "error": f"{kind} claim on {held.key!r} is held",
        "kind": kind, "key": held.key,
        "held_by": held.holder,
        "session": held.session,
        "note": held.note,
        "acquired": held.acquired_at.isoformat(),
        "expires": held.expires_at.isoformat(),
        "advisory": "this claim is advisory: it cannot stop a merge, only warn you",
    }
    if as_given != held.key:
        detail["key_as_given"] = as_given
    return HTTPException(409, detail=detail)


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
    :func:`_canonical_key` — and where the two spellings differ, both are swept
    and both are checked for a holder, because a row predating the board's
    knowledge of that repo is still keyed on the bare one (:func:`_first_held`).
    """
    if body.kind in RESERVED_KINDS:
        raise HTTPException(409, detail={
            "error": f"{body.kind!r} claims are allocated, not taken",
            "kind": body.kind,
            "hint": "use POST /release/claim — see RESERVED_KINDS for why"})

    now = _utcnow()
    sess = _clean(body.session)
    key, named = await _canonical_key(session, body.key)
    # Every spelling this resource can be sitting under, not only the one the
    # write side uses now. See `_key_spellings` and `_first_held`.
    keys = _key_spellings(key, named, body.key)
    for candidate in keys:
        await _sweep_lapsed(session, body.kind, candidate, now)
    await session.commit()

    held = await _first_held(session, body.kind, keys, now)
    if held is not None:
        if not _may_mutate(held, holder, sess):
            raise _conflict(body.kind, held, body.key)
        _renew_onto(held, holder=holder, ttl=body.ttl, sess=sess,
                    note=body.note, now=now)
        await session.commit()
        return _claimed(held, body.key, renewed=True)

    claim = await _take(session, kind=body.kind, key=key, holder=holder,
                        ttl=body.ttl, sess=sess, note=body.note, now=now)
    if claim is None:
        # Lost the insert race. Re-read rather than reporting a generic failure:
        # the loser of a race is exactly the caller who most needs to know who won.
        winner = await _first_held(session, body.kind, keys, now)
        if winner is None:
            contended = {"error": "claim contended; try again",
                         "kind": body.kind, "key": key}
            if body.key != key:
                contended["key_as_given"] = body.key
            raise HTTPException(409, detail=contended)
        if _may_mutate(winner, holder, sess):
            # A real renew, written and committed — not a `renewed: true` over an
            # untouched row. Same request, same reported outcome, same effect,
            # whether or not a race happened to occur (F05).
            _renew_onto(winner, holder=holder, ttl=body.ttl, sess=sess,
                        note=body.note, now=now)
            await session.commit()
            return _claimed(winner, body.key, renewed=True)
        raise _conflict(body.kind, winner, body.key)
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
        # spelling you claimed with would miss the row you just took. Both
        # spellings, for the same reason `take_claim` sweeps all of them: a row
        # taken before the board could identify that repo is still keyed on the
        # bare name, and a lookup that asked only for the canonical form would
        # report a resource free while somebody is visibly holding it.
        canonical, named = await _canonical_key(session, key)
        stmt = stmt.where(ResourceLease.key.in_(_key_spellings(canonical, named, key)))
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

    ``repo`` must already be canonical. This is the one call site that reaches
    into the pre-v2.38 bare-basename bucket (``legacy=True``), because it is the
    one whose job is to be conservative: over-reading there costs a skipped
    number, and under-reading costs a number handed out twice. See
    :func:`_repo_prefix` for why every other caller passes ``legacy=False``.

    The version is read off the END of the key rather than by removing a
    ``repo:`` prefix, because a legacy row is not keyed on the spelling this call
    was made with, and a mis-sliced key parses as nothing and vanishes from the
    floor.
    """
    rows = await session.scalars(
        select(ResourceLease.key)
        .where(ResourceLease.kind == "release", _repo_prefix(repo, legacy=True))
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

    Scoped to the CANONICAL keys only (``legacy=False``). The bare-basename
    bucket is unattributable by construction — ``quarterback:2.9`` could belong to
    either owner of that name — so including it here would let one owner's caller
    renew a row that may be the other's and be handed it back as its own
    idempotent allocation. Ownership is exactly the question a legacy row cannot
    answer, and this function asks nothing else.
    """
    if not sess:
        return None
    mine = await session.scalar(
        select(ResourceLease)
        .where(ResourceLease.kind == "release", _repo_prefix(repo, legacy=False),
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
    ns = _Repo(repo, body.repo)
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
            return _allocated(mine, got, ns, renewed=True, unreadable=unreadable)

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
                    return _allocated(mine, got, ns, renewed=True, unreadable=unreadable)
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
                return _allocated(held, candidate, ns, renewed=True, unreadable=unreadable)
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
        return _allocated(claim, candidate, ns, renewed=False, unreadable=unreadable)

    raise HTTPException(409, detail={
        "error": "could not allocate a release number: the namespace is contended",
        "repo": repo,
        "advice": "retry; several agents are allocating for this repo right now"})


def _another_repo(old_key: str, repo: str) -> HTTPException:
    return HTTPException(409, detail={
        "error": "that claim belongs to another repo",
        "key": old_key, "repo": repo})


async def _reclaim_namespace(session: AsyncSession, given: str, old_key: str) -> str:
    """The namespace a renumber happens in — accepting a claim nobody can expand.

    Renumbering is the one thing a caller does when it is already in trouble, so
    this endpoint has to work for the rows the rest of the fix deliberately walks
    past. Migration 0020 leaves a basename it cannot expand exactly where it is
    (``mysteryrepo:1.4``), and the only spelling that names such a claim is the
    stranded one. Resolving ``repo`` up front and requiring the old claim's head
    to resolve to the same thing locked that holder out twice over: the stranded
    spelling 400s as an unknown repo, and any spelling that does resolve 409s as
    "another repo's claim", because the old head resolves to nothing and nothing
    equals nothing. The pre-v2.38 test was ``old.key.startswith(f"{repo}:")``,
    which accepted exactly that call — so this was a regression into the endpoint
    whose entire purpose is getting a caller off a collision.

    When both sides resolve, they must agree; that is the real ownership check
    and it is unchanged. When either does not, the basenames are compared
    instead, which is what the old prefix test amounted to. Two outcomes follow:

    * the caller named the owner (``acme/mystery`` for ``mystery:1.4``) — the new
      number is taken in the canonical namespace, and ``_highest_known`` reads the
      legacy bucket too, so it cannot land on the stranded number;
    * the OLD claim names the owner and the caller sent a basename the board
      cannot expand (``nsx`` for ``acme/nsx:2.1``) — the old claim settles it, and
      it is a better answer than a 400 even when several owners answer to that
      name, because the caller has already proved it holds this one;
    * nobody can name the owner (``mysteryrepo`` for ``mysteryrepo:1.4``) — the
      new number is taken in the legacy bucket alongside the old one, folded to
      lower case so the two are one bucket. That is the only place the board still
      writes a non-canonical key, and it is confined to a caller who already has
      one and is trying to leave it.
    """
    old_head = split_repo_head(old_key)[0]
    repo, candidates = await _resolve_repo(session, given)
    old_repo, _ = await _resolve_repo(session, old_head)
    if repo is not None and old_repo is not None:
        if repo != old_repo:
            raise _another_repo(old_key, repo)
        return repo
    name = lookup_name(given)
    if name is not None and name == lookup_name(old_head):
        return repo or old_repo or name
    if repo is None:
        raise _unknown_repo(given, candidates)
    raise _another_repo(old_key, repo)


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

    repo = await _reclaim_namespace(session, body.repo, old.key)
    ns = _Repo(repo, body.repo)

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
        return {**_allocated(fresh, candidate, ns, renewed=False, unreadable=unreadable),
                # Named back so the caller can check the swap it just made
                # against the number it has already written into eight files.
                "gave_up": fmt_version(gave_up) if gave_up else None,
                # The key too, always, because `gave_up: null` is otherwise
                # indistinguishable from "no old number" — and it is reachable:
                # a pre-v2.38 row can carry a version tail this board's grammar
                # cannot read. The key says which row was let go regardless.
                "gave_up_key": old_key}

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

    **The rows are this repo's only, while ``highest_known`` also sees the
    pre-v2.38 bucket, so the two can legitimately disagree.** A legacy
    ``quarterback:2.9`` cannot be attributed to an owner, so listing it under
    every repo sharing that basename would report a neighbour's number as this
    one's — but it may still be taken, so the floor must not ignore it. When
    ``highest_known`` exceeds every version listed, that gap is the answer, not a
    bug: something under the old spelling holds the number in between.
    """
    now = _utcnow()
    given = repo
    repo = await _require_repo(session, repo)
    rows = list(await session.scalars(
        select(ResourceLease)
        .where(ResourceLease.kind == "release", _repo_prefix(repo, legacy=False))
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
    # A second query rather than a max over `rows`, and deliberately so: `rows` is
    # capped at `limit` and ordered by acquisition, so the highest version need
    # not be among them, and it excludes the legacy bucket that the floor must
    # include. Computing it from what happens to be in memory would answer a
    # different question and answer it wrong.
    highest = await _highest_known(session, repo)
    out_body = {"repo": repo, "releases": out,
                # What the NEXT call would allocate, absent a higher `after` from
                # the caller. Advisory and racy by nature — reading it is not
                # claiming it, which is the distinction this module is about.
                "highest_known": fmt_version(highest) if highest else None}
    if given != repo:
        out_body["repo_as_given"] = given
    return out_body
