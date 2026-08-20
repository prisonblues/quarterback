"""The plan: what is next, in what order, and who has it.

The board could already say who is here, what they are touching, what they just
published and what the panel found. It could not say **what to do next** — the
question every agent asks first, and the one every agent answered by guessing.
Three agents once fixed the same red CI job in one morning, and the third had
checked for peers and been told the coast was clear: presence said nobody was in
that file, and nothing said the job was already taken.

That knowledge lived in three places, none of them the board — 26 unordered
issues, a human repeating the sequence to whoever asked, and an untracked
``plan.md`` on one machine that no other machine, container or agent could read.

**Four rules, and the whole design is in them:**

1. *It never restates an issue.* An item is a title, a ref and an order. The
   *what* and the *why* stay in GitHub, so the two stores cannot disagree — and
   ``ix_plan_items_open_ref`` makes "one open item per issue" a database fact.
2. *It never decides an item is done.* ``done`` records that the linked issue
   closed; git ancestry and GitHub remain the authority. ``epic.py`` had this
   right first: *"the file is the fast path + audit trail"*.
3. *Only a human reorders it.* If any agent may, the plan thrashes; if only a
   human may, it stays the shared intent it exists to be. Agents add, claim,
   record dependencies and complete. See :func:`app.auth.human`.
4. *It is not a project-management tool.* No estimates, no sprints, no
   burndown, no assignee — a claim with a TTL is the assignee, and it expires.

**Claims are not reimplemented here.** An item is taken when a live
``resource_leases`` row (v2.31) exists for its :func:`claim_key`, so atomicity
comes from that table's partial unique index and expiry is passive — a dead
agent's claim disappears with no reaper and nobody intervening. For an
issue-backed item the key is exactly the ``work`` key agents already take by
hand, so a claim made through the plain ``POST /claim`` shows in the plan
without the claimant doing anything extra.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import (
    DEFAULT_TTL,
    MAX_SESSION,
    MAX_TTL,
    ClaimRequest,
    acquire,
    claim_view,
    clean_session,
    is_unique_violation,
    live_claim,
    may_mutate,
)
from app.auth import human, identify, reader
from app.claimkey import REPO_SHAPE, WORK, BadRef, canonical_repo, derive
from app.db import get_session
from app.identity import same_machine
from app.models.plan import Plan
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease

router = APIRouter(tags=["plan"])

#: Plan claims and hand-taken work claims are the SAME claims, and as of #172
#: that is enforced rather than agreed: both go through :mod:`app.claimkey`, so
#: the two cannot drift. They did drift — an agent holding
#: ``kind='issue', key='<repo>#163'`` was invisible to a plan filtering on
#: ``kind='work'``, and the plan reported ``claimed: 0`` about an issue three
#: agents were holding.
CLAIM_KIND = WORK

#: An open item nobody has touched in this long is reported ``stale``. A plan
#: nobody updates is worse than no plan, because it is believed — so staleness
#: is surfaced rather than left for a reader to work out from a timestamp.
STALE_DAYS = 14

#: Guardrails on the free-text fields. Generous enough for a real title, tight
#: enough that the plan cannot quietly become the place the issue body is
#: duplicated into — which is rule 1, enforced rather than asked for.
MAX_TITLE = 200
MAX_NOTE = 2000
MAX_DEPS = 32
#: A plan label is a handle an agent says out loud, not a description. The old
#: ``phase`` column was bounded at 64 on the wire and this keeps that.
MAX_LABEL = 64
#: Most items one ``POST /plan/submit`` may carry. A plan is tens of rows by
#: design (rule 4), and the atomicity this endpoint exists for is a single
#: transaction — an unbounded batch would hold the scope lock for as long as the
#: caller cared to make it.
MAX_SUBMIT = 64

#: The advisory-lock key every dependency write takes. An arbitrary constant —
#: what matters is only that all of them agree on it. Nothing else in this board
#: uses advisory locks, so it collides with nothing.
_DEPS_LOCK = 0x504C414E  # "PLAN"

#: The first half of the per-scope order lock. ``pg_advisory_xact_lock`` also
#: takes two int4s, so ``(_RANK_LOCK, hash(scope))`` is one lock per repo: the
#: rank of an item is only meaningful beside its neighbours', and two writers
#: computing "the next rank" or rewriting "1..n" from the same snapshot is a lost
#: update either way. Per scope rather than global, because reordering one repo's
#: list has nothing to say about another's.
_RANK_LOCK = 0x52414E4B  # "RANK"

#: Postgres advisory-lock keys are signed 32-bit. Python's ``hash`` is neither
#: bounded nor stable across processes, so the scope key is derived from a digest
#: instead — two containers must agree on which lock a repo maps to.
_INT32 = 0x7FFFFFFF


def _utcnow() -> datetime:
    return datetime.now(UTC)


def claim_key(item: PlanItem) -> str:
    """The ``resource_leases`` key that means "this item is taken".

    **Derived, in one place, shared with every other claim path** — see
    :mod:`app.claimkey`. This function used to compose the string itself, and
    that was one of the two implementations #172 found disagreeing: it produced
    the issue key correctly and had no idea what an agent typing ``kind='issue'``
    by hand produced.

    An issue-backed item uses the key agents already take by hand
    (``prisonblues/quarterback#142``), so the plan sees claims it never mediated.
    A PR-backed item now gets ``<repo>!<n>`` rather than falling back to its own
    id — the same reason: a PR claimed by hand is a claim the plan should be able
    to see, and ``!`` keeps it clear of the issue numbered the same. An item with
    no ref at all is keyed by its own id, because there is nothing else to key it
    by.
    """
    if item.repo and item.ref_kind in ("issue", "pr") and item.ref_value:
        try:
            _, key = derive(item.ref_kind, repo=item.repo, value=item.ref_value)
            return key
        except BadRef:
            # A row written before `_norm_scope` refused a malformed repo (or an
            # unparseable ref) still has to be READABLE. Falling back to the item
            # key means such an item is claimable and joins nothing — which is
            # exactly what it was before, and strictly better than a plan read
            # that 500s over one bad row and shows nobody anything.
            pass
    _, key = derive("item", value=item.id)
    return key


def plan_claim_key(plan: Plan) -> str:
    """The key that means "this whole plan is taken" (#172).

    Claiming a plan is how the one genuinely fuzzy race is covered: two agents
    surveying the same vague problem, before any item exists to claim. It is the
    same table, the same TTL and the same passive expiry as an item claim —
    coarser, and deliberately the only coarse grain there is.
    """
    _, key = derive("plan", value=plan.id)
    return key


def _norm_scope(repo: str | None) -> str | None:
    """``""`` is not a repo — it is a third scope nothing agrees on.

    The unique index keys on ``COALESCE(repo, '')``, so an empty-string repo
    collides with the fleet items; :func:`claim_key` reads it as falsy and keys
    the item by id; and ``_next_rank`` ranks it as a scope of its own. One
    normalisation at the edge, and the three cannot disagree.

    Lower-cased for the same reason, one level up: GitHub repository names are
    case-insensitive, so ``Acme/Repo`` and ``acme/repo`` are one repo everywhere
    except here, where they would pass the uniqueness index as two open items
    with two claim keys — "one open item per issue" defeated by a shift key.
    """
    if repo is None:
        return None
    if not repo.strip():
        return None
    try:
        return canonical_repo(repo)
    except BadRef:
        # Refused rather than stored, because from #172 onward the repo is half of
        # a derived claim key: a bare `quarterback` beside a
        # `prisonblues/quarterback` is the two-spellings defect back again, one
        # level down, and it would key the same issue two ways. The lower-casing
        # this used to do was necessary and not sufficient — it made `Acme/Repo`
        # and `acme/repo` agree and left `repo` and `acme/repo` disagreeing.
        raise HTTPException(422, REPO_SHAPE) from None


def _norm_text(value: str | None) -> str | None:
    """Free text as it will be READ: stripped, and blank is absent.

    ``min_length=1`` passes ``" "``, and a plan row whose title renders as an
    empty span gives an agent reading ``next`` nothing to act on. Every other
    field that had to mean one thing (repo, ref) is normalised at the edge; the
    one a human actually reads was the one left alone.
    """
    if value is None:
        return None
    return value.strip() or None


def _norm_ref(value: str | None) -> str | None:
    """``"#60"`` and ``"60"`` are the same issue, and must not be two items."""
    if value is None:
        return None
    return value.strip().lstrip("#").strip() or None


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _load(session: AsyncSession, ids: set[str]) -> dict[str, PlanItem]:
    """Fetch items by id string, skipping anything that isn't a uuid."""
    parsed = [u for u in (_as_uuid(i) for i in ids) if u is not None]
    if not parsed:
        return {}
    rows = await session.scalars(select(PlanItem).where(PlanItem.id.in_(parsed)))
    return {str(r.id): r for r in rows}


def _dep_refused(token: str, problem: str, hint: str) -> HTTPException:
    """One shape for every dependency refusal.

    Two shapes was one shape too many: the issue branch answered with
    ``{"error": …, "hint": …}`` and the item-id branch with a bare string, so
    every consumer — the plan page included — had to type-test ``detail`` before
    it could show the reason.
    """
    return HTTPException(422, detail={
        "error": f"depends_on {token!r}: {problem}", "token": token, "hint": hint})


async def _resolve_dep(session: AsyncSession, token: str, repo: str | None) -> PlanItem:
    """One dependency, given as an item id or as ``#60`` / ``60``.

    Issue numbers are accepted because that is how agents and humans actually
    talk about the work; they resolve to the item that references them, so what
    is stored is always an item id and the graph never depends on a spelling.

    Both spellings resolve to an OPEN item, and that has to be both: only an open
    item can block anything (``_item_view`` shows blockers filtered to open), so
    a dependency on a dropped item is an edge that has no effect and never will.
    Refusing it by issue number while accepting it by uuid answered the same
    question two ways and handed back a 200 for a link that did nothing.
    """
    as_uuid = _as_uuid(token)
    if as_uuid is not None:
        item = await session.get(PlanItem, as_uuid)
        if item is None:
            raise _dep_refused(token, "no such plan item",
                               "dependencies are links between plan items — add it first")
        if item.state != "open":
            raise _dep_refused(
                token, f"that item is {item.state}",
                "only an open item can block: a dropped or finished one would be a "
                "dependency that never resolves and never shows")
        return item
    ref = _norm_ref(token)
    if not ref:
        raise _dep_refused(token, "not an item id or an issue number",
                           "spell it as an item id or as an issue like '#60'")
    stmt = select(PlanItem).where(
        PlanItem.ref_value == ref, PlanItem.ref_kind == "issue", PlanItem.state == "open")
    if repo is not None:
        stmt = stmt.where(or_(PlanItem.repo == repo, PlanItem.repo.is_(None)))
    else:
        # A fleet item's "#15" is NOT "whichever repo happens to have a 15".
        # Unscoped, this matched any repo's item, so a dependency could silently
        # bind to an unrelated repo's issue and block on work nobody meant.
        stmt = stmt.where(PlanItem.repo.is_(None))
    # An exact-repo match beats a fleet item that happens to carry the same number.
    item = await session.scalar(stmt.order_by(PlanItem.repo.is_(None), PlanItem.rank).limit(1))
    if item is None:
        raise _dep_refused(
            token, "nothing open in the plan references that issue",
            "add the item it depends on first — the plan links to issues, "
            "so a dependency is a link between items, not a bare number")
    return item


async def _resolve_deps(session: AsyncSession, raw: list[str] | None, repo: str | None,
                        item_id: uuid.UUID | None) -> list[str]:
    """Dependency tokens → item ids: existing, de-duplicated, and acyclic."""
    if not raw:
        return []
    if len(raw) > MAX_DEPS:
        raise HTTPException(422, detail={
            "error": f"at most {MAX_DEPS} dependencies per item",
            "hint": "an item waiting on thirty others is a plan, not an item"})
    if item_id is not None:
        # Held from here to the commit, so the graph this validates against is
        # the graph the write lands on. NOT taken on the add path: nothing can
        # reference an id that does not exist yet, so a new item's edges cannot
        # close a cycle — which is precisely why `_refuse_cycle` is skipped there
        # as well. Taking it anyway serialised every `plan_add` that carried
        # dependencies behind one global lock, against a race it cannot have.
        await _lock_deps(session)
    resolved: list[str] = []
    for token in raw:
        dep = await _resolve_dep(session, token, repo)
        if dep.id == item_id:
            raise _dep_refused(token, "an item cannot depend on itself",
                               "nothing would ever unblock it")
        if str(dep.id) not in resolved:
            resolved.append(str(dep.id))
    if item_id is not None:
        await _refuse_cycle(session, item_id, resolved)
    return resolved


async def _lock_deps(session: AsyncSession) -> None:
    """Serialise dependency writes, so two of them cannot each validate as acyclic.

    Read-committed is not enough on its own: two transactions adding A→B and
    B→A each read a graph without the other's edge, both pass the cycle check,
    and both commit — leaving a cycle no single request was ever wrong to make.
    A transaction-scoped advisory lock is the cheapest correct fix; it is held
    only across a dependency write, which is a rare, tiny transaction, and it is
    released by the commit or rollback whatever happens.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DEPS_LOCK})


async def _refuse_cycle(session: AsyncSession, item_id: uuid.UUID, deps: list[str]) -> None:
    """Refuse a dependency edge that would make "what is unblocked?" unanswerable.

    Walked in memory over the OPEN items: a plan is tens of rows, and a recursive
    CTE to avoid one small SELECT would be the more expensive mistake. The
    open-only filter is what keeps that true — the table is append-only, done
    items are never deleted and the history features encourage collecting them,
    so an unfiltered scan grows without bound while holding the one global
    advisory lock every dependency write queues on. Nothing is lost by it:
    only an open item blocks (:func:`_item_view` filters blockers the same way),
    so a cycle through a finished item cannot make anything unanswerable.

    Call under :func:`_lock_deps`, or the check is only as good as its timing.
    """
    rows = await session.execute(
        select(PlanItem.id, PlanItem.depends_on).where(PlanItem.state == "open"))
    graph = {str(i): list(d or []) for i, d in rows}
    graph[str(item_id)] = deps
    seen, stack = set(), list(deps)
    while stack:
        node = stack.pop()
        if node == str(item_id):
            raise HTTPException(
                422, f"that dependency is circular: {node} already waits on this item")
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))


async def _lock_scope(session: AsyncSession, repo: str | None) -> None:
    """Serialise the writers that compute a rank from the ranks they can see.

    ``_next_rank`` reads ``max(rank)`` and inserts ``max + 1``; ``reorder`` reads
    the list and rewrites it 1..n. Both are read-then-write with nothing between
    them, and there is no unique index on ``(repo, rank)`` to catch the
    collision — two adds land on the same rank, or two reorders each overwrite
    the other's decision, and the plan quietly stops being a total order.

    It also fixes the lock ORDER for a reorder: without it, two rewrites of the
    same rows in opposite sequences take row locks in opposite sequences, which
    is a deadlock waiting for two people with the plan page open.
    """
    scope = int.from_bytes(hashlib.sha256((repo or "").encode()).digest()[:4], "big") & _INT32
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:a, :b)"), {"a": _RANK_LOCK, "b": scope})


async def _claims_for(session: AsyncSession, keys: set[str],
                      now: datetime) -> dict[str, ResourceLease]:
    """The live claim on each key, in one query rather than one per row.

    Takes keys rather than items so plans and items come back from the same
    query: they are the same kind on the same table, and two round trips to look
    up one row each was two chances for the reads to disagree about ``now``.
    """
    if not keys:
        return {}
    rows = await session.scalars(
        select(ResourceLease).where(
            ResourceLease.kind == CLAIM_KIND, ResourceLease.key.in_(keys),
            ResourceLease.released_at.is_(None), ResourceLease.expires_at > now)
    )
    return {r.key: r for r in rows}


def _plan_view(plan: Plan, claim: ResourceLease | None, now: datetime,
               items: dict[str, int] | None = None) -> dict:
    idle = (now - plan.updated_at).total_seconds() / 86400
    return {
        "plan_id": str(plan.id),
        "repo": plan.repo,
        "label": plan.label,
        "note": plan.note,
        "state": plan.state,
        "claim": claim_view(claim) if claim is not None else None,
        "added_by": plan.added_by,
        "created": plan.created_at.isoformat(),
        "updated": plan.updated_at.isoformat(),
        "idle_days": round(idle, 1),
        "stale": plan.state == "open" and idle >= STALE_DAYS,
        "done": plan.done_at.isoformat() if plan.done_at else None,
        "done_by": plan.done_by,
        **({"items": items} if items is not None else {}),
    }


def _covered_by(claim: ResourceLease | None, mine: str | None,
                session_id: str | None = None) -> dict | None:
    """The plan-level claim standing over this item, if it is somebody ELSE's.

    An agent that claimed a whole plan has said "all of this is mine", and
    offering its items to the next caller as free work is the duplicated work the
    claim was taken to prevent. Reported as its own field rather than folded into
    ``claim``: the item itself is genuinely unclaimed, and saying it is claimed
    would make ``plan_release`` on it a 404 that reads like a bug.

    Your own plan claim covers nothing from you — it is what lets you work through
    your own plan item by item.

    **"Yours" is the session's when the caller says which session it is.** A plan
    claim is session-owned (that is the whole of #142's rule: a machine runs
    several agents on one token and they are different agents), so answering by
    machine alone told a co-tenant that its neighbour's held plan was free —
    the exact duplicated work the claim prevents, restored on the read path. The
    machine is still necessary and is the fallback: ``GET /plan`` authorises with
    ``reader``, which resolves a bearer token to a machine and knows nothing finer,
    so a caller that sends no ``session`` gets the coarser honest answer rather
    than a wrong one.
    """
    if claim is None:
        return None
    if mine and same_machine(claim.holder, mine):
        wanted = clean_session(session_id)
        if not claim.session or not wanted or wanted == claim.session:
            return None
    return {"holder": claim.holder, "session": claim.session, "note": claim.note,
            "expires": claim.expires_at.isoformat()}


def _item_view(item: PlanItem, claim: ResourceLease | None,
               blockers: list[PlanItem], now: datetime,
               plan: Plan | None = None, plan_claim: ResourceLease | None = None,
               mine: str | None = None, session_id: str | None = None) -> dict:
    idle = (now - item.updated_at).total_seconds() / 86400
    return {
        "item_id": str(item.id),
        "repo": item.repo,
        "title": item.title,
        "ref": {"kind": item.ref_kind, "value": item.ref_value} if item.ref_kind else None,
        "plan": ({"plan_id": str(plan.id), "label": plan.label, "state": plan.state,
                  "claim": claim_view(plan_claim) if plan_claim is not None else None}
                 if plan is not None else None),
        "covered_by": _covered_by(plan_claim, mine, session_id),
        "rank": item.rank,
        "state": item.state,
        "note": item.note,
        "depends_on": list(item.depends_on or []),
        # Only OPEN dependencies block: a dropped one will never be done, and
        # waiting on it forever would be the plan quietly lying about "next".
        "blocked_by": [{"item_id": str(b.id), "title": b.title,
                        "ref": b.ref_value, "repo": b.repo} for b in blockers],
        "claim": claim_view(claim) if claim is not None else None,
        "added_by": item.added_by,
        "created": item.created_at.isoformat(),
        "updated": item.updated_at.isoformat(),
        "idle_days": round(idle, 1),
        "stale": item.state == "open" and idle >= STALE_DAYS,
        "done": item.done_at.isoformat() if item.done_at else None,
        "done_by": item.done_by,
    }


async def _plans_for(session: AsyncSession, items: list[PlanItem]) -> dict[str, Plan]:
    """The plan row behind each item that names one, keyed by plan id."""
    ids = {i.plan_id for i in items if i.plan_id is not None}
    if not ids:
        return {}
    rows = await session.scalars(select(Plan).where(Plan.id.in_(ids)))
    return {str(r.id): r for r in rows}


async def _plans_view(session: AsyncSession, repo: str | None, exact: bool,
                      now: datetime, mine: str | None = None,
                      include_closed: bool = False,
                      session_id: str | None = None) -> list[dict]:
    """The open plans in scope, with their claims and how many items each holds.

    Rendered on every plan read rather than behind its own endpoint, because the
    question "is somebody already surveying this" is the one an agent has to ask
    BEFORE it starts, and an answer that needs a second call is an answer agents
    do not fetch. #172's evidence is a fleet where nobody called the primitive at
    all.
    """
    stmt = select(Plan)
    if not include_closed:
        stmt = stmt.where(Plan.state == "open")
    if exact:
        stmt = stmt.where(Plan.repo.is_(None) if repo is None else Plan.repo == repo)
    elif repo is not None:
        stmt = stmt.where(or_(Plan.repo == repo, Plan.repo.is_(None)))
    plans = list(await session.scalars(
        stmt.order_by(Plan.state != "open", Plan.repo.is_(None), Plan.repo,
                      Plan.created_at, Plan.id)))
    if not plans:
        return []
    claims = await _claims_for(
        session, {plan_claim_key(p) for p in plans if p.state == "open"}, now)
    counts = {plan_id: n for plan_id, n in await session.execute(
        select(PlanItem.plan_id, func.count())
        .where(PlanItem.plan_id.in_([p.id for p in plans]), PlanItem.state == "open")
        .group_by(PlanItem.plan_id))}
    return [
        _plan_view(p, claims.get(plan_claim_key(p)) if p.state == "open" else None, now,
                   items={"open": counts.get(p.id, 0)})
        | {"covered_by": _covered_by(
            claims.get(plan_claim_key(p)) if p.state == "open" else None, mine,
            session_id)}
        for p in plans
    ]


async def _view_items(session: AsyncSession, items: list[PlanItem], now: datetime,
                      mine: str | None = None,
                      session_id: str | None = None) -> list[dict]:
    """Render items with their live claims, their plan, and their open blockers.

    A claim attaches to an OPEN item only. Claims are keyed by ``repo#issue``, so
    an issue that was finished and later re-added shares its key with the item
    that replaced it — and a history read then showed the new item's live claim
    sitting on the old done row, which reads as "this finished work is currently
    being worked on by somebody".

    ``mine`` is the reader's identity, and it is what makes ``covered_by`` mean
    anything: a plan claim held by the caller covers nothing from the caller.
    """
    plans = await _plans_for(session, items)
    open_items = [i for i in items if i.state == "open"]
    claims = await _claims_for(
        session,
        {claim_key(i) for i in open_items}
        | {plan_claim_key(p) for p in plans.values() if p.state == "open"},
        now)
    known = {str(i.id): i for i in items}
    wanted = {d for i in items for d in (i.depends_on or [])} - set(known)
    known |= await _load(session, wanted)

    def plan_of(item: PlanItem) -> Plan | None:
        return plans.get(str(item.plan_id)) if item.plan_id is not None else None

    def plan_claim_of(plan: Plan | None) -> ResourceLease | None:
        if plan is None or plan.state != "open":
            return None
        return claims.get(plan_claim_key(plan))

    return [
        _item_view(
            item, claims.get(claim_key(item)) if item.state == "open" else None,
            [known[d] for d in (item.depends_on or [])
             if d in known and known[d].state == "open"],
            now,
            plan=plan_of(item),
            plan_claim=plan_claim_of(plan_of(item)) if item.state == "open" else None,
            mine=mine, session_id=session_id,
        )
        for item in items
    ]


async def _scope_items(session: AsyncSession, repo: str | None, exact: bool,
                       include_done: bool, limit: int | None = None,
                       plan_id: uuid.UUID | None = None) -> list[PlanItem]:
    stmt = select(PlanItem)
    if plan_id is not None:
        # Filtered in SQL, ahead of the LIMIT. Filtering the page afterwards
        # dropped every matching item past the first `limit` rows — and with it
        # `next`, which would read as "nothing to do in this plan".
        stmt = stmt.where(PlanItem.plan_id == plan_id)
    if exact:
        stmt = stmt.where(PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo)
    elif repo is not None:
        # A read for one repo also gets the fleet-wide items: they are part of
        # what is next for anybody, and an agent that never sees them is exactly
        # the agent the fleet scope exists for.
        stmt = stmt.where(or_(PlanItem.repo == repo, PlanItem.repo.is_(None)))
    if not include_done:
        stmt = stmt.where(PlanItem.state == "open")
    # Open work first, then history — a finished item keeps the rank it had, so
    # without this a history read leads with items from three weeks ago and can
    # push live ones past `limit`.
    #
    # Then the SCOPE BAND, and this is the rule that had no answer before it.
    # Ranks are allocated per scope: a repo's list is 1..n and the fleet's is its
    # own 1..n, so ordering the merged read by rank alone interleaved two
    # sequences no human had ever compared — a fleet item ranked 1 outranked
    # every repo item but the first, and equal ranks fell to whichever was
    # inserted first. The rule now is stated and testable: **your repo's list
    # first, then the fleet's**. A repo read is about that repo, and the
    # fleet list is what you pick up when your own has nothing free — `next`
    # falls through into it rather than being preempted by it.
    #
    # Then rank, then insertion order: rank is per scope and rewritten
    # wholesale, so a tiebreak keeps the list total even mid-rewrite.
    stmt = stmt.order_by(PlanItem.state != "open", PlanItem.repo.is_(None),
                         PlanItem.repo, PlanItem.rank, PlanItem.created_at, PlanItem.id)
    if limit is not None:
        # Truncation is from the BOTTOM of the list, never the top: the plan is
        # ordered, so the items that matter are the first ones, and a read that
        # dropped those would answer "what is next" with the wrong item. A
        # reorder passes no limit — a rewrite must see the whole scope.
        stmt = stmt.limit(limit)
    return list(await session.scalars(stmt))


async def _next_rank(session: AsyncSession, repo: str | None) -> int:
    """The rank an appended item takes. Call under :func:`_lock_scope`."""
    stmt = select(PlanItem.rank).order_by(PlanItem.rank.desc()).limit(1)
    stmt = stmt.where(PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo)
    return (await session.scalar(stmt) or 0) + 1


async def _counts_by_state(session: AsyncSession, repo: str | None,
                           plan_id: uuid.UUID | None, exact: bool) -> dict[str, int]:
    """How many items in this scope are in each state — over the WHOLE scope.

    An aggregate rather than a count of the page: history is the unbounded half
    of this table, and `done`/`dropped` counted off a `limit`-truncated read
    reported "3 finished" for a repo with three hundred.
    """
    stmt = select(PlanItem.state, func.count()).group_by(PlanItem.state)
    if plan_id is not None:
        stmt = stmt.where(PlanItem.plan_id == plan_id)
    if exact:
        stmt = stmt.where(PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo)
    elif repo is not None:
        stmt = stmt.where(or_(PlanItem.repo == repo, PlanItem.repo.is_(None)))
    return {state: n for state, n in await session.execute(stmt)}


async def _get(session: AsyncSession, item_id: uuid.UUID) -> PlanItem:
    item = await session.get(PlanItem, item_id)
    if item is None:
        raise HTTPException(404, "plan item not found")
    return item


def _in_scope(plan: Plan, repo: str | None) -> bool:
    """May a caller working in ``repo`` name this plan?

    Its own repo, or the fleet. A fleet plan is reachable from every scope by
    design — that is what the NULL scope is — and a repo plan is reachable only
    from its own, because "move this item into that plan" across repos would put
    a row in a list nobody reading that repo can see.
    """
    return plan.repo is None or plan.repo == repo


async def _find_plan(session: AsyncSession, token: str | None, repo: str | None,
                     *, open_only: bool) -> Plan | None:
    """The plan a caller named, by id or by label. None if it named none, or none matched.

    Case-folded on the label, which is the whole reason a plan is a row: "stage
    1" and "Stage 1" were two phases and nothing could tell. The index enforces
    it for writes; this is the same rule for reads, so a caller cannot fail to
    find the plan it just created by capitalising it differently.

    **The id path takes the same two filters as the label path**, and that is a
    fix rather than tidiness: a bare ``session.get`` accepted a *closed* plan and
    *another repo's*, so ``plan_item/update`` would move an item into a finished
    list, or into one nobody reading that repo can see. An id is not an
    authorisation to skip the rules the name has to pass.
    """
    token = _norm_text(token)
    if not token:
        return None
    as_uuid = _as_uuid(token)
    if as_uuid is not None:
        plan = await session.get(Plan, as_uuid)
        if plan is None or not _in_scope(plan, repo):
            return None
        return None if open_only and plan.state != "open" else plan
    stmt = select(Plan).where(func.lower(Plan.label) == token.lower())
    if open_only:
        stmt = stmt.where(Plan.state == "open")
    # By label the scope is EXACT rather than widened: two scopes may each hold an
    # open "stage 1" (the unique index is per scope), so widening would make which
    # one you got depend on insertion order.
    stmt = stmt.where(Plan.repo.is_(None) if repo is None else Plan.repo == repo)
    return await session.scalar(stmt.order_by(Plan.created_at).limit(1))


async def _plan_or_422(session: AsyncSession, token: str | None, repo: str | None,
                       *, open_only: bool = True) -> Plan | None:
    if token is None:
        return None
    plan = await _find_plan(session, token, repo, open_only=open_only)
    if plan is None:
        raise HTTPException(422, detail={
            "error": f"no {'open ' if open_only else ''}plan called {token!r} "
                     f"in this scope",
            "repo": repo, "plan": token,
            "hint": "a plan is a row now, not a string on an item (#172): submit it "
                    "with POST /plan/submit, or list them with GET /plans"})
    return plan


async def _ensure_plan(session: AsyncSession, label: str | None, repo: str | None,
                       author: str, note: str | None = None) -> Plan | None:
    """The open plan with this label in this scope, creating it if there is none.

    Find-or-create, and not the strictness it looks like it is missing. Refusing
    an unknown label would make ``plan_add(plan="stage 2")`` a two-call dance for
    the commonest thing an agent does, and the discipline #172 asks for is that
    there be exactly ONE row per label — which the case-folded unique index
    provides whether the row was made here or by ``POST /plan/submit``. What is
    gone is the free-text field, not the convenience.
    """
    label = _norm_text(label)
    if not label:
        return None
    plan = await _find_plan(session, label, repo, open_only=True)
    if plan is not None:
        return plan
    plan = Plan(repo=repo, label=label, note=_norm_text(note), added_by=author)
    session.add(plan)
    try:
        await session.flush()
    except IntegrityError as e:
        await session.rollback()
        if not is_unique_violation(e):
            raise
        # Somebody created it between the lookup and the insert. Theirs is the
        # real one — the index is what makes "one plan per label" true, and this
        # is the losing side of it doing the only correct thing.
        existing = await _find_plan(session, label, repo, open_only=True)
        if existing is None:  # pragma: no cover — the index just said otherwise
            raise
        return existing
    return plan


class ItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    repo: str | None = Field(default=None, max_length=256)
    ref_kind: Literal["issue", "pr"] | None = None
    #: ``"60"`` or ``"#60"`` — normalised, so one issue cannot become two items.
    ref_value: str | None = Field(default=None, max_length=64)
    #: The plan this item belongs to, by label or by id. This replaced the
    #: free-text ``phase`` (#172): the same field on the wire, with a row behind
    #: it. An unknown label creates the plan — see :func:`_ensure_plan` for why
    #: that is not the laxness it looks like.
    plan: str | None = Field(default=None, max_length=MAX_LABEL)
    #: WHY it sits here. The sentence a human would otherwise repeat to each
    #: agent that asks, which is the half an issue has no field for.
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    #: Item ids, or issue refs (``"#55"``) resolved against the same repo.
    depends_on: list[str] = Field(default_factory=list)


class ItemRefIn(BaseModel):
    item_id: uuid.UUID


class ClaimItemIn(ItemRefIn):
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    #: Bounded like every other free-text field that reaches a stored row — these
    #: three flow straight into `resource_leases.session`, which `ClaimIn` bounds
    #: and these had opted out of for no reason anyone had stated.
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    note: str | None = Field(default=None, max_length=500)
    #: Take a blocked item anyway. The refusal is advice, not a gate — but it
    #: has to be said out loud, so "I know it is blocked" is in the record.
    force: bool = False


class ReleaseItemIn(ItemRefIn):
    session: str | None = Field(default=None, max_length=MAX_SESSION)


class DoneIn(ItemRefIn):
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    note: str | None = Field(default=None, max_length=MAX_NOTE)


class DependsIn(ItemRefIn):
    depends_on: list[str] = Field(default_factory=list)


class UpdateIn(ItemRefIn):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    #: Move the item to another plan, by label or id. The empty string detaches
    #: it — a loose item is a real state (it is what every item was before v2.39
    #: gave them phases), so there has to be a way back to it.
    plan: str | None = Field(default=None, max_length=MAX_LABEL)
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    state: Literal["open", "dropped"] | None = None


class SubmitItemIn(BaseModel):
    """One line of a plan being submitted. No ``repo`` and no ``plan``: both come
    from the submission, so an item cannot land in a different scope from the plan
    that carries it."""

    title: str = Field(min_length=1, max_length=MAX_TITLE)
    ref_kind: Literal["issue", "pr"] | None = None
    ref_value: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    #: Item ids, issue refs (``"#55"``), or ``"@2"`` — the second item of THIS
    #: submission. The last of those is what makes a plan submittable as a unit:
    #: an ordered plan whose edges can only point at rows that already exist is a
    #: plan that has to be written twice.
    depends_on: list[str] = Field(default_factory=list)


class SubmitIn(BaseModel):
    label: str = Field(min_length=1, max_length=MAX_LABEL)
    repo: str | None = Field(default=None, max_length=256)
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    items: list[SubmitItemIn] = Field(min_length=1, max_length=MAX_SUBMIT)
    #: Take the plan in the same call. The surveying agent almost always wants
    #: this — it wrote the plan, and the window between submitting and claiming is
    #: exactly the window a second agent raids.
    claim: bool = True
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    note_on_claim: str | None = Field(default=None, max_length=500)


class PlanRefIn(BaseModel):
    plan_id: uuid.UUID


class ClaimPlanIn(PlanRefIn):
    ttl: int = Field(default=DEFAULT_TTL, ge=1, le=MAX_TTL)
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    note: str | None = Field(default=None, max_length=500)


class ReleasePlanIn(PlanRefIn):
    session: str | None = Field(default=None, max_length=MAX_SESSION)


class DonePlanIn(PlanRefIn):
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    #: Finish a plan that still has open items. Refused by default and said out
    #: loud when used: "the plan is done and six items are not" is a fact worth a
    #: deliberate keystroke.
    force: bool = False


class ReorderIn(BaseModel):
    #: The scope being reordered, EXACTLY: ``null`` reorders the fleet-wide
    #: items, not everything. A read widens (repo + fleet) because context
    #: helps; a write narrows, because a rewrite that silently included another
    #: scope's items would be the plan reordering itself behind your back.
    repo: str | None = None
    order: list[uuid.UUID] = Field(min_length=1)


@router.get("/plan")
async def read_plan(
    repo: str | None = Query(default=None, description="this repo's items plus the fleet-wide ones"),
    include_done: bool = Query(default=False, description="include done and dropped items"),
    plan: str | None = Query(default=None,
                            description="only this plan, by label or id"),
    exact: bool = Query(default=False,
                        description="this scope ONLY — do not widen a repo read to the "
                                    "fleet-wide items (and, with no repo, the fleet list "
                                    "by itself)"),
    limit: int = Query(default=200, ge=1, le=1000,
                       description="most items to return, from the TOP of the order"),
    session_q: str | None = Query(default=None, alias="session",
                                  description="your session id, so a plan claim held by "
                                              "a CO-TENANT on your machine reads as "
                                              "somebody else's rather than as yours"),
    caller: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What is next, in order, with who has what — the one call an agent makes cold.

    ``next`` is the answer to the actual question: the first item that is open,
    unclaimed, unblocked and not inside a plan somebody else is holding. An agent
    that reads nothing else still gets a truthful answer, and one that reads the
    list sees why the items above it were skipped (held, blocked, or covered by
    another agent's plan claim).
    """
    now = _utcnow()
    repo = _norm_scope(repo)
    scoped = await _plan_or_422(session, plan, repo, open_only=False)
    plan_id = scoped.id if scoped is not None else None
    # `next` and `counts` are answers about the PLAN; `items` is a page of it.
    # Deriving all three from one truncated query made the first two describe the
    # page instead: with the first `limit` open items claimed or blocked, `next`
    # said "nothing is free" while free work sat at rank limit+1 — the one
    # failure this endpoint exists to prevent — and the header the board page
    # renders verbatim silently under-counted every scope larger than the cap.
    #
    # The open set is what a limit is not for: it is bounded by design (a plan is
    # tens of rows; four rules keep it that way), while history is what grows.
    # So the open set is read whole, and `limit` truncates the page alone.
    open_items = await _scope_items(session, repo, exact=exact, include_done=False,
                                    plan_id=plan_id)
    open_views = await _view_items(session, open_items, now, mine=caller,
                                   session_id=session_q)
    if include_done:
        views = await _view_items(
            session,
            await _scope_items(session, repo, exact=exact, include_done=True,
                               limit=limit, plan_id=plan_id),
            now, mine=caller, session_id=session_q)
    else:
        views = open_views[:limit]
    unclaimed = [v for v in open_views
                 if not v["claim"] and not v["blocked_by"] and not v["covered_by"]]
    by_state = await _counts_by_state(session, repo, plan_id, exact)
    in_scope = sum(by_state.values()) if include_done else by_state.get("open", 0)
    plans = await _plans_view(session, repo, exact, now, caller,
                              session_id=session_q)
    # The narrowed plan comes OUT of that list rather than being rendered again, so
    # `plan` and the matching row of `plans` cannot disagree about who holds it —
    # rendering it separately gave it `claim: null` while the list showed the claim.
    # A plan narrowed to but out of scope (a closed one, or another repo's) still
    # answers with its own view; it is just claimless, which it is.
    scoped_view = next((row for row in plans
                        if scoped is not None and row["plan_id"] == str(scoped.id)),
                       _plan_view(scoped, None, now) if scoped is not None else None)
    return {
        "repo": repo,
        "exact": exact,
        "plan": scoped_view,
        "plans": plans,
        "generated": now.isoformat(),
        "items": views,
        # Said out loud rather than left to be worked out by comparing lengths:
        # a caller that got a page is entitled to know it was one.
        "truncated": len(views) < in_scope,
        "next": unclaimed[0] if unclaimed else None,
        "counts": {
            "open": len(open_views),
            "claimed": sum(1 for v in open_views if v["claim"]),
            "blocked": sum(1 for v in open_views if v["blocked_by"]),
            # Held via a plan claim rather than item by item. Counted separately
            # because the remedy is different: a blocked item needs work
            # finishing, a covered one needs a word with its holder.
            "covered": sum(1 for v in open_views if v["covered_by"]),
            "stale": sum(1 for v in open_views if v["stale"]),
            "done": by_state.get("done", 0),
            "dropped": by_state.get("dropped", 0),
        },
    }


@router.post("/plan/item")
async def add_item(
    body: ItemIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Append an item. Adding is not reordering, so an agent may do it.

    A second open item for an issue already in the plan is refused, naming the
    one that is already there — the plan holding two rows about #60 is precisely
    the drift it exists to remove.
    """
    if (body.ref_kind is None) != (_norm_ref(body.ref_value) is None):
        raise HTTPException(422, "a ref needs both kind and value, or neither")
    title = _norm_text(body.title)
    if not title:
        raise HTTPException(422, "a title is what an agent reads in `next`: it cannot be blank")
    ref_value = _norm_ref(body.ref_value)
    repo = _norm_scope(body.repo)
    deps = await _resolve_deps(session, body.depends_on, repo, item_id=None)
    plan = await _ensure_plan(session, body.plan, repo, author)
    # Held to the commit: `_next_rank` is a read-then-insert, and two adds in one
    # scope both reading the same maximum is a lost update with no unique index
    # behind it to notice — two items at the same position, ordered thereafter by
    # whichever happened to be created first.
    await _lock_scope(session, repo)
    item = PlanItem(
        repo=repo, title=title, ref_kind=body.ref_kind, ref_value=ref_value,
        plan_id=plan.id if plan is not None else None,
        note=_norm_text(body.note), depends_on=deps,
        added_by=author, rank=await _next_rank(session, repo),
    )
    session.add(item)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        # ONLY the duplicate-ref index. A check-constraint violation is a real
        # fault, and reporting it as "already in the plan" would send the caller
        # looking for an item that does not exist.
        if not is_unique_violation(e):
            raise
        existing = await session.scalar(
            select(PlanItem).where(
                PlanItem.ref_kind == body.ref_kind, PlanItem.ref_value == ref_value,
                PlanItem.state == "open",
                PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo)
        )
        raise HTTPException(409, detail={
            "error": f"{body.ref_kind} {ref_value} is already in the plan",
            "item_id": str(existing.id) if existing else None,
            "title": existing.title if existing else None,
            "hint": "the plan links to issues and never restates them, so one open "
                    "item per issue — reorder or update that one instead",
        }) from None
    await session.refresh(item)
    return (await _view_items(session, [item], _utcnow()))[0]


@router.post("/plan/item/claim")
async def claim_item(
    body: ClaimItemIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take an item, visibly to every other agent, with a TTL that frees it.

    The claim is the same row ``POST /claim`` writes, so this cannot disagree
    with the claims table and a crashed holder's item comes back on its own.

    **Owned by the session, not by the box.** The claims table's default is that
    a claim belongs to the machine, which is right for a land — an agent that
    restarts mid-land must pick its own claim back up. It is wrong here, and
    wrong in the exact way this feature exists to fix: a machine runs several
    agents at once and they all authenticate as that one token, so the box rule
    answered a second agent's claim with ``renewed: true`` and let both of them
    work the same item. Three agents fixing the same CI job, moved indoors.
    """
    now = _utcnow()
    item = await _get(session, body.item_id)
    if item.state != "open":
        raise HTTPException(409, detail={
            "error": f"that item is {item.state}", "item_id": str(item.id)})
    blockers = await _blockers_for(session, item)
    if blockers and not body.force:
        raise HTTPException(409, detail={
            "error": "that item is waiting on unfinished work",
            "item_id": str(item.id), "blocked_by": blockers,
            "hint": "pass force=true if you mean to take it anyway"})
    # **A claim blocks. It is not a note to read past** (#172). Reporting the plan
    # hold on the read path and then letting this call take the item anyway is
    # exactly the state the issue is about: a record everybody can see and nothing
    # honours. `force` is the escape, because the plan holder may genuinely be
    # sharing the work — but then "I know somebody holds the plan" is on the record.
    covering = await _covering_claim(session, item, holder, body.session, now)
    if covering is not None and not body.force:
        raise HTTPException(409, detail={
            "error": "the plan this item belongs to is held by somebody else",
            "item_id": str(item.id), "covered_by": covering,
            "hint": "they said the whole plan was theirs — talk to them (their "
                    "session is above), or pass force=true to take one item out of "
                    "it deliberately. If your plan read offered this as free work, "
                    "you did not send `session` on GET /plan: a plan claim is owned "
                    "by the session, so without one that read can only answer by "
                    "machine and a co-tenant's hold looks like your own"})
    claim, renewed = await acquire(session, ClaimRequest(
        kind=CLAIM_KIND, key=claim_key(item), holder=holder, ttl=body.ttl,
        sess=body.session, note=_claim_note(item, body.note, blockers), now=now,
        session_owned=True))
    # The checks above and the claim are two statements, and the world can move
    # between them. Nothing can lock across `acquire` (it commits — that is where
    # its atomicity comes from), so each check is made again afterwards and the
    # claim handed straight back if it lost.
    #
    # Two things can have moved. The item can have been finished or dropped: a
    # claim on a dropped item is a claim nobody can act on and nobody can see. And
    # somebody can have taken the whole PLAN in the same window — which is the
    # narrower race, and the one that matters more, because "both claims live" is
    # two agents each correctly believing the work is theirs. That is the exact
    # outcome the plan claim exists to prevent, so losing it here costs the item
    # claim rather than being reported as a warning nobody reads.
    await session.refresh(item)
    if item.state != "open":
        claim.released_at = now
        await session.commit()
        raise HTTPException(409, detail={
            "error": f"that item became {item.state} while you were claiming it",
            "item_id": str(item.id), "hint": "re-read the plan: it moved under you"})
    if not body.force:
        raced = await _covering_claim(session, item, holder, body.session, now)
        if raced is not None:
            claim.released_at = now
            await session.commit()
            raise HTTPException(409, detail={
                "error": "somebody took the whole plan while you were claiming this item",
                "item_id": str(item.id), "covered_by": raced,
                "hint": "re-read the plan: it moved under you. Talk to them, or pass "
                        "force=true to take one item out of it deliberately"})
    # One instant per request: `now` stamps the claim, the row and the view it
    # renders, so a single logical moment is not three slightly different ones.
    item.updated_at = now
    await session.commit()
    view = (await _view_items(session, [item], now))[0]
    return {**view, "claimed": True, "renewed": renewed, "claim_id": str(claim.id),
            "forced": bool(blockers)}


async def _covering_claim(session: AsyncSession, item: PlanItem, holder: str,
                          session_id: str | None, now: datetime) -> dict | None:
    """Somebody else's live claim on this item's PLAN, or None.

    Ownership is decided by :func:`_is_mine`, the same function that decides
    whether you may release or complete an item — so "my plan" means exactly what
    it means everywhere else on this router, session and all.
    """
    if item.plan_id is None:
        return None
    plan = await session.get(Plan, item.plan_id)
    if plan is None or plan.state != "open":
        return None
    claim = await live_claim(session, CLAIM_KIND, plan_claim_key(plan), now)
    if claim is None or _is_mine(claim, holder, session_id):
        return None
    return {"plan_id": str(plan.id), "label": plan.label, "holder": claim.holder,
            "session": claim.session, "note": claim.note,
            "expires": claim.expires_at.isoformat()}


async def _blockers_for(session: AsyncSession, item: PlanItem) -> list[dict]:
    """Just the open items this one waits on — without rendering the whole view.

    ``claim_item`` used to build the full item view twice per request (a claims
    query and a blocker load each time) to read one field out of the first one.
    """
    known = await _load(session, set(item.depends_on or []))
    return [{"item_id": str(b.id), "title": b.title, "ref": b.ref_value, "repo": b.repo}
            for b in (known.get(d) for d in (item.depends_on or []))
            if b is not None and b.state == "open"]


def _claim_note(item: PlanItem, note: str | None, blockers: list[dict]) -> str:
    """What the claim says it is for — and, if it was forced, that it was.

    ``force`` is documented as "the refusal is advice, but it has to be said out
    loud, so 'I know it is blocked' is in the record". It was not in any record:
    the stored note was the caller's or a generic fallback, so a forced claim and
    an ordinary one were indistinguishable a day later.
    """
    said = _norm_text(note) or f"plan: {item.title}"
    if not blockers:
        return said[:500]
    waiting = ", ".join(f"#{b['ref']}" if b["ref"] else b["title"] for b in blockers)
    return f"[forced past {waiting}] {said}"[:500]


@router.post("/plan/item/release")
async def release_item(
    body: ReleaseItemIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Let an item go. Idempotent: nothing held is a fine answer, not an error."""
    now = _utcnow()
    item = await _get(session, body.item_id)
    released = await _release_claim(session, item, holder, body.session, now)
    if released:
        # Only when something actually changed. `updated_at` is the sole input to
        # `stale`, so bumping it on a no-op let any caller keep an abandoned item
        # looking fresh forever by releasing a claim it never held — hiding
        # exactly the item the staleness flag exists to surface.
        item.updated_at = now
        await session.commit()
    return {**(await _view_items(session, [item], now))[0], "released": released}


def _is_mine(claim: ResourceLease, holder: str, session_id: str | None) -> bool:
    """Is this plan claim the CALLER's — the caller being an agent, not a box?

    :func:`may_mutate` is the claims table's rule and stays as it is. A plan
    claim adds the session, for the reason the whole feature exists: two agents
    on one machine are two workers, and letting either release or complete the
    other's item is the same duplicated-work failure as letting both hold it. A
    claim that recorded no session falls back to the machine — there is nothing
    finer to compare, and stranding claims taken by callers that sent none would
    be a worse answer than trusting the box.
    """
    if not may_mutate(claim, holder, session_id):
        return False
    return not claim.session or clean_session(session_id) == claim.session


async def _release_claim(session: AsyncSession, item: PlanItem, holder: str,
                         session_id: str | None, now: datetime) -> bool:
    """Drop the live claim on an item if it is this caller's. True if one went."""
    claim = await live_claim(session, CLAIM_KIND, claim_key(item), now)
    if claim is None:
        return False
    if not _is_mine(claim, holder, session_id):
        raise HTTPException(403, detail={
            "error": "that claim is not yours", "held_by": claim.holder,
            "session": claim.session, "note": claim.note,
            "hint": "a plan claim belongs to the session that took it: two agents "
                    "on one machine are two workers"})
    claim.released_at = now
    return True


@router.post("/plan/item/done")
async def complete_item(
    body: DoneIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record that an item is finished — and release its claim in the same breath.

    **This does not decide anything.** The issue closing is what makes the work
    done; this records that it happened so the next agent's plan read is not one
    item out of date. If the two ever disagree, the issue is right.

    A claim held by somebody else is left alone and reported: an item another
    agent is holding might be finished by a third, but taking their claim away
    is not this endpoint's business. That claim keeps its key until it lapses or
    its holder lets go, so a re-added item for the same issue can find it still
    live — which is the truth being reported, not a leak: somebody is still
    working on that issue and the plan should say so.

    **Any agent may record it, deliberately.** Elsewhere this module is careful
    about who may act (only the holder releases, only a human drops), and the
    asymmetry is the second rule: `done` is not a decision. The issue closing is
    the fact; whoever observes it may write it down, and refusing a non-holder
    would mean an agent that watched the PR merge has to wait for a lapsed claim
    before the plan stops offering finished work. The response names the claim
    that was left, so a disagreement is legible rather than silent.
    """
    item = await _get(session, body.item_id)
    if item.state == "dropped":
        # A drop is a human decision that this should NOT happen. Letting an
        # agent finish it anyway would route around the one rule the human-only
        # endpoints exist to keep — quietly, and in the record.
        raise HTTPException(409, detail={
            "error": "a human dropped this item", "item_id": str(item.id),
            "hint": "if the work happened anyway, ask for it to be reopened first"})
    now = _utcnow()
    claim = await live_claim(session, CLAIM_KIND, claim_key(item), now)
    mine = claim is not None and _is_mine(claim, holder, body.session)
    left = None if mine or claim is None else claim_view(claim)
    if claim is not None and mine:
        claim.released_at = now
    if item.state != "done":
        item.state, item.done_at, item.done_by = "done", now, holder
    item.note = _completion_note(item.note, body.note)
    item.updated_at = now
    await session.commit()
    view = (await _view_items(session, [item], now))[0]
    # The claim itself, not a bool: `done` no longer renders a claim on the item
    # (it is history), so "somebody else was holding this when it was recorded
    # finished" would otherwise be a fact with nowhere left to read it.
    return {**view, "claim_left": left}


def _completion_note(existing: str | None, said: str | None) -> str | None:
    """Add the completion note to the item's note without destroying it.

    `note` is the human's reasoning for the item's position — "the sentence a
    human would otherwise repeat to each agent that asks", and human-only to
    edit for exactly that reason. Replacing it with a completing agent's receipt
    ("landed in PR #143") deleted the intent and left the receipt in a field the
    agent was not allowed to write, unrecoverably.
    """
    said = _norm_text(said)
    if not said:
        return existing
    merged = f"{existing}\n— done: {said}" if existing else said
    return merged[-MAX_NOTE:] if len(merged) > MAX_NOTE else merged


@router.post("/plan/item/depends")
async def set_depends(
    body: DependsIn,
    _: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record what an item is waiting on. An agent may: a dependency is a FACT.

    The split that runs through this module — order and intent are the human's,
    observations are the fleet's. An agent that discovers #57's cost column
    cannot be built before #15 should be able to write that down where the next
    agent will see it, without being able to decide the sequence.
    """
    item = await _get(session, body.item_id)
    if item.state != "open":
        # The same rule the other verbs keep: an item that is finished or dropped
        # is history, and history does not acquire new reasons to wait.
        raise HTTPException(409, detail={
            "error": f"that item is {item.state}", "item_id": str(item.id),
            "hint": "only an open item can be waiting on something"})
    now = _utcnow()
    item.depends_on = await _resolve_deps(session, body.depends_on, item.repo, item.id)
    item.updated_at = now
    await session.commit()
    return (await _view_items(session, [item], now))[0]


@router.post("/plan/item/update")
async def update_item(
    body: UpdateIn,
    editor: str = Depends(human),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Retitle, move, re-reason, or drop an item. Human-only, like reordering.

    ``dropped`` is not ``done``: one says the work happened, the other says a
    person decided it should not. Reopening a dropped item is allowed here too,
    because a decision reversed is still a decision.

    A ``done`` item cannot be dropped. Dropping clears ``done_at``/``done_by``,
    so the drop control on a history row was one click from destroying the record
    that the issue ever closed — and the page offered it on every row.
    """
    item = await _get(session, body.item_id)
    if body.state is not None and item.state == "done" and body.state != "done":
        raise HTTPException(409, detail={
            "error": "that item is done: finished work is a record, not a plan item",
            "item_id": str(item.id),
            "hint": "if the work was undone, add a new item for it — dropping this "
                    "one would erase who finished it and when"})
    if body.title is not None:
        title = _norm_text(body.title)
        if not title:
            raise HTTPException(422, "a title cannot be blank")
        item.title = title
    if body.plan is not None:
        # "" detaches. Any other value must name a plan that exists: a human
        # moving an item is making a decision about an object, and inventing one
        # off a typo is how "stage 1" and "Stage 1" happened in the first place.
        moved = await _plan_or_422(session, body.plan, item.repo) if body.plan.strip() \
            else None
        item.plan_id = moved.id if moved is not None else None
    if body.note is not None:
        item.note = _norm_text(body.note)
    now = _utcnow()
    if body.state is not None and body.state != item.state:
        if body.state == "dropped":
            # A human deciding this should not happen has to free whoever is
            # holding it: the item disappears from every plan read, and a claim
            # nobody can see is a claim that blocks the issue's next item until
            # its TTL runs out, held by an agent working on something cancelled.
            held = await live_claim(session, CLAIM_KIND, claim_key(item), now)
            if held is not None:
                held.released_at = now
        item.state = body.state
        item.done_at, item.done_by = None, None
    item.updated_at = now
    # Read off the identity BEFORE the write: after a failed commit the instance
    # is expired and touching it re-runs the flush that just failed.
    ref = (item.repo, item.ref_kind, item.ref_value, item.id)
    try:
        await session.commit()
    except IntegrityError as e:
        # Reopening an item whose issue has since been taken by a NEWER item
        # trips "one open item per ref". A 500 would be the wrong answer to a
        # question that has a right one: say which item is in the way.
        await session.rollback()
        if not is_unique_violation(e):
            raise
        raise await _ref_taken(session, *ref) from None
    return {**(await _view_items(session, [item], now))[0], "edited_by": editor}


async def _ref_taken(session: AsyncSession, repo: str | None, ref_kind: str | None,
                     ref_value: str | None, exclude: uuid.UUID) -> HTTPException:
    """409 naming the open item that already holds this ref.

    Takes plain values rather than the instance: this runs after a failed
    commit, where the ORM object is expired and reading it would re-emit the
    flush that just failed.
    """
    existing = await session.scalar(
        select(PlanItem).where(
            PlanItem.ref_kind == ref_kind, PlanItem.ref_value == ref_value,
            PlanItem.state == "open", PlanItem.id != exclude,
            PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo)
    )
    return HTTPException(409, detail={
        "error": f"{ref_kind} {ref_value} is already open in the plan",
        "item_id": str(existing.id) if existing else None,
        "title": existing.title if existing else None,
        "hint": "one open item per issue — drop or finish that one first"})


@router.post("/plan/reorder")
async def reorder(
    body: ReorderIn,
    editor: str = Depends(human),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set the order. **Human-only** — this is the endpoint decision 1 is about.

    Items in scope that the caller did not list keep their relative order and
    follow the listed ones, and are named in ``appended``. A stale page must not
    be able to lose an item it never knew about — but it must not silently
    reshuffle it either, so the omission is reported rather than assumed.

    **Only open items have an order.** A dropped item used to be re-ranked by
    every reorder and named in ``appended`` while being absent from the ``items``
    the same response returned — so the page, the request and the reply each
    believed something different about a row nobody could see.
    """
    repo = _norm_scope(body.repo)
    # Taken before the read, so the list this rewrites is the list it validated
    # against and two rewrites of one scope cannot interleave. It also fixes the
    # order rows are locked in: two callers reordering the same items in opposite
    # sequences deadlocked each other otherwise.
    await _lock_scope(session, repo)
    items = await _scope_items(session, repo, exact=True, include_done=False)
    by_id = {str(i.id): i for i in items}
    strays = [str(i) for i in body.order if str(i) not in by_id]
    if strays:
        raise HTTPException(422, detail={
            "error": "those items are not open in this scope",
            "repo": repo, "items": strays,
            "hint": "order is for open work: finished and dropped items keep no place"})
    ordered = [by_id[str(i)] for i in dict.fromkeys(str(i) for i in body.order)]
    rest = [i for i in by_id.values() if i not in ordered]
    now = _utcnow()
    for rank, item in enumerate([*ordered, *rest], start=1):
        if item.rank != rank:
            item.rank, item.updated_at = rank, now
    await session.commit()
    return {
        "repo": repo, "reordered": len(ordered), "by": editor,
        "appended": [str(i.id) for i in rest],
        "items": await _view_items(
            session, await _scope_items(session, repo, exact=True, include_done=False), now),
    }


# ------------------------------------------------------------- plans, as rows


#: ``"@2"`` — the second item of this submission. Chosen because ``@`` cannot
#: start a uuid or an issue ref, so the three token forms `_resolve_deps` accepts
#: stay unambiguous without a mode flag.
_BATCH_REF = "@"


def _batch_deps(items: list[SubmitItemIn]) -> list[list[int]]:
    """The intra-submission edges, as 0-based indices. Refuses a cycle.

    Checked in memory and BEFORE anything is written, which is the whole reason
    this endpoint exists: a plan that lands half-written is a plan a second agent
    can raid, and "refuse the submission" is only available while nothing has been
    inserted.

    Only the ``@`` edges can cycle. An edge pointing at an existing item cannot
    close one, because an existing item's own ``depends_on`` cannot contain an id
    that did not exist when it was written — so the graph reachable from a new
    item through old ones is acyclic by construction.
    """
    edges: list[list[int]] = []
    for position, item in enumerate(items):
        mine: list[int] = []
        for token in item.depends_on:
            if not str(token).startswith(_BATCH_REF):
                continue
            body = str(token)[len(_BATCH_REF):].strip()
            if not body.isdigit() or not 1 <= int(body) <= len(items):
                raise _dep_refused(
                    str(token), f"not an item of this submission (1..{len(items)})",
                    f"'{_BATCH_REF}2' means the second item you are submitting")
            index = int(body) - 1
            if index == position:
                raise _dep_refused(str(token), "an item cannot depend on itself",
                                   "nothing would ever unblock it")
            if index not in mine:
                mine.append(index)
        edges.append(mine)
    _refuse_batch_cycle(edges)
    return edges


def _refuse_batch_cycle(edges: list[list[int]]) -> None:
    """Depth-first over the submission's own edges; 422 on the first cycle."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = [WHITE] * len(edges)

    def walk(node: int, trail: list[int]) -> None:
        colour[node] = GREY
        for nxt in edges[node]:
            if colour[nxt] == GREY:
                cycle = " → ".join(f"@{n + 1}" for n in [*trail, node, nxt])
                raise HTTPException(422, detail={
                    "error": f"those dependencies are circular: {cycle}",
                    "hint": "nothing in that ring would ever unblock"})
            if colour[nxt] == WHITE:
                walk(nxt, [*trail, node])
        colour[node] = BLACK

    for node in range(len(edges)):
        if colour[node] == WHITE:
            walk(node, [])


@router.get("/plans")
async def list_plans(
    repo: str | None = Query(default=None, description="this repo's plans plus the fleet's"),
    exact: bool = Query(default=False, description="this scope ONLY"),
    include_closed: bool = Query(default=False, description="include done and dropped plans"),
    session_q: str | None = Query(default=None, alias="session",
                                  description="your session id — see GET /plan"),
    caller: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The plans in scope, with their claims and their open item counts.

    The read an agent makes before it starts surveying, to find out whether
    somebody already is.
    """
    now = _utcnow()
    repo = _norm_scope(repo)
    return {"repo": repo, "exact": exact, "generated": now.isoformat(),
            "plans": await _plans_view(session, repo, exact, now, caller,
                                       include_closed=include_closed,
                                       session_id=session_q)}


@router.post("/plan/submit")
async def submit_plan(
    body: SubmitIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A whole plan, in ONE transaction, claimed on the way out (#172).

    **Why this is not a loop over ``POST /plan/item``.** It was, and that was the
    defect: an eight-item plan landed incrementally, so a second agent reading the
    plan between item three and item four saw a plan that was not the plan and
    could claim from it. That is the same race the claim primitive exists to close,
    moved earlier and made worse — the raider is not even wrong, because what it
    read really was the plan at that moment.

    So the unit of submission is the unit of intent: the plan row, every item, and
    every dependency between them commit together or not at all. ``claim=true``
    (the default) then takes the plan in the same call, because the surveying
    agent wrote it and the gap between writing and holding is the gap.

    The plan's label must be free in its scope. An existing open plan is a 409
    naming it rather than an append — "add to that one" and "submit a plan" are
    different intentions, and quietly merging them is how a raided plan would look
    like a successful submission.
    """
    now = _utcnow()
    repo = _norm_scope(body.repo)
    label = _norm_text(body.label)
    if not label:
        raise HTTPException(422, "a plan needs a label: it is what agents say out loud")
    titles = [_norm_text(i.title) for i in body.items]
    if not all(titles):
        raise HTTPException(422, detail={
            "error": "every item needs a title",
            "items": [n + 1 for n, t in enumerate(titles) if not t],
            "hint": "a title is what an agent reads in `next`: it cannot be blank"})
    refs = [_norm_ref(i.ref_value) for i in body.items]
    for n, (item, ref) in enumerate(zip(body.items, refs, strict=True), start=1):
        if (item.ref_kind is None) != (ref is None):
            raise HTTPException(422, detail={
                "error": f"item {n}: a ref needs both kind and value, or neither"})
    seen: dict[tuple[str | None, str], int] = {}
    for n, (item, ref) in enumerate(zip(body.items, refs, strict=True), start=1):
        if ref is None:
            continue
        first = seen.setdefault((item.ref_kind, ref), n)
        if first != n:
            # Caught here rather than at the index, because the index would report
            # it as "already in the plan" about a row this very request created —
            # a message that sends the caller looking for somebody else's item.
            raise HTTPException(422, detail={
                "error": f"items {first} and {n} both reference {item.ref_kind} {ref}",
                "hint": "one open item per issue: the plan links to issues and never "
                        "restates them"})

    batch = _batch_deps(body.items)

    existing = await _find_plan(session, label, repo, open_only=True)
    if existing is not None:
        raise HTTPException(409, detail={
            "error": f"a plan called {existing.label!r} is already open here",
            "plan_id": str(existing.id), "repo": existing.repo,
            "hint": "add to it with POST /plan/item (plan=<label>), or finish it "
                    "first — submitting over it would be two plans with one name"})

    # Held to the commit: every item's rank comes off `_next_rank`, and a
    # submission is the case where that read-then-insert happens `len(items)`
    # times in a row.
    await _lock_scope(session, repo)
    plan = Plan(repo=repo, label=label, note=_norm_text(body.note), added_by=author)
    session.add(plan)
    rank = await _next_rank(session, repo)
    plan_items: list[PlanItem] = []
    for position, (item, title, ref) in enumerate(
            zip(body.items, titles, refs, strict=True)):
        plan_items.append(PlanItem(
            repo=repo, title=title, ref_kind=item.ref_kind, ref_value=ref,
            note=_norm_text(item.note), added_by=author, rank=rank + position,
            depends_on=[]))
    for row in plan_items:
        session.add(row)
    try:
        # Ids first: an `@2` edge needs the id of a row that has not been assigned
        # one yet, and a plan whose edges are written in a second transaction is
        # the incremental plan this endpoint replaces.
        await session.flush()
    except IntegrityError as e:
        await session.rollback()
        if not is_unique_violation(e):
            raise
        raise await _submit_conflict(session, repo, body.items, refs) from None
    for row in plan_items:
        row.plan_id = plan.id
    # External edges resolve against the database, batch edges against this
    # submission, and the two merge in the order the caller wrote them.
    for position, (row, item) in enumerate(zip(plan_items, body.items, strict=True)):
        outside = [t for t in item.depends_on if not str(t).startswith(_BATCH_REF)]
        resolved = await _resolve_deps(session, outside, repo, item_id=row.id)
        inside = [str(plan_items[i].id) for i in batch[position]]
        row.depends_on = list(dict.fromkeys([*resolved, *inside]))
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if not is_unique_violation(e):
            raise
        raise await _submit_conflict(session, repo, body.items, refs) from None

    claimed = None
    if body.claim:
        # After the commit, never inside it: `acquire` commits (that is where its
        # atomicity comes from) and refuses a session with pending work.
        claim, _ = await acquire(session, ClaimRequest(
            kind=CLAIM_KIND, key=plan_claim_key(plan), holder=author, ttl=body.ttl,
            sess=body.session,
            note=_norm_text(body.note_on_claim) or f"planning: {plan.label}",
            now=now, session_owned=True))
        claimed = claim_view(claim)
    await session.refresh(plan)
    return {
        **_plan_view(plan, None, now, items={"open": len(plan_items)}),
        "claim": claimed,
        "items": await _view_items(session, list(plan_items), now, mine=author),
    }


async def _submit_conflict(session: AsyncSession, repo: str | None,
                           asked: list[SubmitItemIn], refs: list[str | None],
                           ) -> HTTPException:
    """409 naming which of the submitted refs is already open in the plan.

    A submission is all-or-nothing, so the caller needs to know *which* line to
    change — "something in there collides" would make it bisect its own plan.
    """
    clashes = []
    for item, ref in zip(asked, refs, strict=True):
        if ref is None:
            continue
        held = await session.scalar(
            select(PlanItem).where(
                PlanItem.ref_kind == item.ref_kind, PlanItem.ref_value == ref,
                PlanItem.state == "open",
                PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo))
        if held is not None:
            clashes.append({"ref": f"{item.ref_kind} {ref}", "item_id": str(held.id),
                            "title": held.title})
    return HTTPException(409, detail={
        "error": "some of those refs are already open in the plan"
                 if clashes else "that submission collided with an existing row",
        "clashes": clashes,
        "hint": "one open item per issue — nothing was written, so drop those lines "
                "and submit again",
    })


async def _get_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    return plan


@router.post("/plan/claim")
async def claim_plan(
    body: ClaimPlanIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take a whole plan — "all of this is mine", and the planning pass itself.

    The one coarse grain in the system, and #172 argues for exactly one: two
    agents surveying the same vague problem in parallel is the only genuinely
    fuzzy race left, because there are no items yet to be exact about. Everything
    downstream of a plan is item keys.

    Session-owned, like an item claim and for the same reason: a machine runs
    several agents on one token, and "two agents on one box both hold the plan" is
    the failure it exists to prevent.
    """
    now = _utcnow()
    plan = await _get_plan(session, body.plan_id)
    if plan.state != "open":
        raise HTTPException(409, detail={
            "error": f"that plan is {plan.state}", "plan_id": str(plan.id)})
    claim, renewed = await acquire(session, ClaimRequest(
        kind=CLAIM_KIND, key=plan_claim_key(plan), holder=holder, ttl=body.ttl,
        sess=body.session, note=_norm_text(body.note) or f"planning: {plan.label}",
        now=now, session_owned=True))
    # Re-read after `acquire` commits: a human can drop a plan between the state
    # check and the claim, and a claim on a plan nobody can see blocks its key
    # until the TTL runs out. The same correction `claim_item` makes, for the same
    # reason — `acquire` cannot be held inside a lock.
    await session.refresh(plan)
    if plan.state != "open":
        claim.released_at = now
        await session.commit()
        raise HTTPException(409, detail={
            "error": f"that plan became {plan.state} while you were claiming it",
            "plan_id": str(plan.id), "hint": "re-read the plans: it moved under you"})
    plan.updated_at = now
    await session.commit()
    return {**_plan_view(plan, claim, now), "claimed": True, "renewed": renewed,
            "claim_id": str(claim.id)}


@router.post("/plan/release")
async def release_plan(
    body: ReleasePlanIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Let a plan go. Idempotent: holding nothing is a fine answer, not an error."""
    now = _utcnow()
    plan = await _get_plan(session, body.plan_id)
    claim = await live_claim(session, CLAIM_KIND, plan_claim_key(plan), now)
    released = False
    if claim is not None:
        if not _is_mine(claim, holder, body.session):
            raise HTTPException(403, detail={
                "error": "that plan claim is not yours", "held_by": claim.holder,
                "session": claim.session, "note": claim.note,
                "hint": "a plan claim belongs to the session that took it: two "
                        "agents on one machine are two workers"})
        claim.released_at = now
        released = True
        # Only when something changed — `updated_at` is the sole input to `stale`,
        # and bumping it on a no-op would let any caller keep an abandoned plan
        # looking fresh by releasing a claim it never held.
        plan.updated_at = now
        await session.commit()
    return {**_plan_view(plan, None, now), "released": released}


@router.post("/plan/done")
async def complete_plan(
    body: DonePlanIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record that a plan is finished, and let its claim go with it.

    Rule 2 applies here as it does to an item: this does not *decide* anything,
    it records what happened, so any agent may write it. What it does check is
    arithmetic — a plan with open items left is refused unless ``force``, because
    "finished" and "six items outstanding" cannot both be true and the plan is
    what the next agent reads.

    **What ``force`` leaves behind, said out loud so it is not read as an
    oversight.** The plan closes and its open items stay open — they are named in
    ``items_left`` and they go back to being ordinary free work, offered by
    ``next``, because a closed plan can no longer cover anything. That is the
    intended reading of "the plan is over and these were not done": the items are
    still worth doing and nobody is holding them. Drop them instead if they should
    not happen — that is a different verb, and a human's.
    """
    now = _utcnow()
    plan = await _get_plan(session, body.plan_id)
    if plan.state == "dropped":
        raise HTTPException(409, detail={
            "error": "a human dropped this plan", "plan_id": str(plan.id),
            "hint": "if the work happened anyway, ask for it to be reopened first"})
    left = list(await session.scalars(
        select(PlanItem).where(PlanItem.plan_id == plan.id, PlanItem.state == "open")))
    if left and not body.force:
        raise HTTPException(409, detail={
            "error": f"{len(left)} item(s) in that plan are still open",
            "plan_id": str(plan.id),
            "items": [{"item_id": str(i.id), "title": i.title, "ref": i.ref_value}
                      for i in left],
            "hint": "finish or drop them first, or pass force=true to close the plan "
                    "over them"})
    claim = await live_claim(session, CLAIM_KIND, plan_claim_key(plan), now)
    mine = claim is not None and _is_mine(claim, holder, body.session)
    if claim is not None and mine:
        claim.released_at = now
    if plan.state != "done":
        plan.state, plan.done_at, plan.done_by = "done", now, holder
    plan.note = _completion_note(plan.note, body.note)
    plan.updated_at = now
    await session.commit()
    return {**_plan_view(plan, None, now),
            "claim_left": None if mine or claim is None else claim_view(claim),
            "items_left": [str(i.id) for i in left]}
