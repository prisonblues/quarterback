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
3. *Only a human reorders it — and PLACING a new item is not reordering (#183).*
   Permuting items already in the plan is contested: two agents disagreeing about
   whether #80 outranks #83 and rewriting each other is how the plan stops being
   the shared intent it exists to be, so ``POST /plan/reorder`` is human-only and
   stays that way. Choosing where a NEW item *enters* alters the relative order of
   nothing already there — every existing pair keeps its existing relationship —
   so it cannot thrash, and ``after`` / ``before`` on ``POST /plan/item`` let an
   agent do it. What a placement competes with is not another agent's judgement;
   it is this endpoint's own hard-coded "last", which nobody chose. Agents add,
   place, claim, record dependencies and complete. See :func:`app.auth.human`.
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
from typing import Literal, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text, tuple_, update
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
from app.models.order_proposal import OrderProposal
from app.models.plan import Plan
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease
from app.models.review import ReviewFinding, ReviewFindingOutcome, ReviewRun
from app.ordering import BASES, Candidate, Ordering, moves_between, suggest_order

router = APIRouter(tags=["plan"])

#: Plan claims and hand-taken work claims are the SAME claims, and as of #172
#: that is enforced rather than agreed: both go through :mod:`app.claimkey`, so
#: the two cannot drift. They did drift — an agent holding
#: ``kind='issue', key='<repo>#163'`` was invisible to a plan filtering on
#: ``kind='work'``, and the plan reported ``claimed: 0`` about an issue three
#: agents were holding.
CLAIM_KIND = WORK

#: The one input here that can go stale, and the only one that is not a row in
#: this database: CI status and PR state are what a panel *saw when it ran*.
#: Past this many days the run is still used — it remains the best evidence there
#: is — but the item is named in ``unknown``, so a reader can see that part of the
#: order rests on last week. Shorter than :data:`STALE_DAYS` on purpose: those
#: measure two different things, one a plan nobody is tending and the other a
#: fact about a branch that moves several times a day.
EVIDENCE_STALE_DAYS = 7

#: The most items an order will be computed for. Not a page size — an order is
#: not pageable, and a proposal missing items would be read as one about the whole
#: scope — so past this line both endpoints REFUSE rather than truncate. The plan
#: is bounded by design (its four rules keep it to tens of rows), which is what
#: makes a refusal the cheap answer here: a scope with five hundred open items has
#: a problem this endpoint is not the place to work around.
#:
#: Applied in :func:`_compute_order` rather than at the writer, so the read and
#: the record answer the same way. Refusing to store a 600-item proposal while
#: cheerfully serving one was two answers to one question, and the served one
#: would have been the answer anybody actually used.
MAX_ORDER_ENTRIES = 500

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
#: Provenance for a placed item — "Rich, 2026-08-17 23:00". An attribution, not a
#: justification: the reasoning still goes in ``note``, and a field long enough to
#: hold an argument would collect one.
MAX_PLACED_FOR = 120
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


def _too_many_deps(position: int | None = None) -> HTTPException:
    """The cap on how much one item may wait on, refused in ONE shape.

    ``POST /plan/item/depends`` refused anything over :data:`MAX_DEPS` and a
    submission did not: only the ``outside`` tokens went through
    :func:`_resolve_deps`, and the ``@n`` edges were merged in afterwards, so one
    submitted row could land holding 32 + 63 of them — through the endpoint whose
    whole point is that a plan arrives as a unit, and against the same row the
    other endpoint would have refused. Counted on the tokens as ASKED FOR, which
    is where :func:`_resolve_deps` counts them too: before de-duplication, so the
    answer does not depend on how many of them were the same edge written twice.
    """
    return HTTPException(422, detail={
        "error": f"{f'item {position}: ' if position is not None else ''}"
                 f"at most {MAX_DEPS} dependencies per item",
        **({"item": position} if position is not None else {}),
        "hint": "an item waiting on thirty others is a plan, not an item"})


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
        raise _too_many_deps()
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


def _hand_back(claim: ResourceLease, renewed: bool, now: datetime) -> bool:
    """Give up the claim THIS request took — and only this request's. Returns kept.

    The re-checks after :func:`acquire` exist to undo a claim that lost a race
    while it was being taken. But ``acquire`` may have RENEWED a claim the caller
    already held before the request ever arrived, and releasing that one is
    confiscating a legitimate claim as collateral: the caller held the item,
    somebody else took the enclosing plan, and it now holds neither — which is
    strictly worse than the state the re-check is there to prevent, because the
    work really was this agent's. So a renew is left standing and reported back in
    ``claim_kept``, and only a claim this request created is handed in.
    """
    if renewed:
        return True
    claim.released_at = now
    return False


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


async def _plan_activity(session: AsyncSession,
                         plans: list[Plan]) -> dict[uuid.UUID, datetime]:
    """When each plan's ITEMS last moved — the other half of "is this plan alive?".

    ``stale`` is derived from ``plans.updated_at``, and a plan is worked through
    its items: appending one, claiming one, releasing one, recording a dependency,
    finishing one and moving one in from another plan all leave the plan row
    untouched. Only ``plan/claim``, ``plan/release`` and ``plan/done`` ever
    touched it — so a plan whose items were being worked through daily reported
    ``stale: true`` after a fortnight, which is the exact opposite of what the
    flag is for. "A plan that is believed and wrong is worse than no plan" cuts
    both ways: a live plan called stale is as misleading as a dead one called
    fresh.

    **Derived on the read rather than bumped on every item write**, deliberately,
    and it is one mechanism rather than two. There are eight item-write paths that
    would each have to remember (``reorder`` touches items in several plans at
    once), an opt-out set is a second place to forget — and every item claim would
    then take a row lock on the plan it belongs to, serialising the agents the
    plan exists to keep apart. One query, and it cannot drift from the writes
    because it reads them.
    """
    ids = [p.id for p in plans]
    if not ids:
        return {}
    rows = await session.execute(
        select(PlanItem.plan_id, func.max(PlanItem.updated_at))
        .where(PlanItem.plan_id.in_(ids)).group_by(PlanItem.plan_id))
    return {plan_id: latest for plan_id, latest in rows if latest is not None}


def _plan_view(plan: Plan, claim: ResourceLease | None, now: datetime,
               items: dict[str, int] | None = None,
               active_at: datetime | None = None) -> dict:
    """One plan as it reads, given when anything inside it last moved.

    ``active_at`` is what keeps ``stale`` honest — see :func:`_plan_activity`. It
    is folded into ``updated`` as well as into ``idle_days``, because two fields
    of one response disagreeing about when a plan last changed is this release's
    own defect in miniature: one question, two answers.
    """
    latest = plan.updated_at if active_at is None else max(plan.updated_at, active_at)
    idle = (now - latest).total_seconds() / 86400
    return {
        "plan_id": str(plan.id),
        "repo": plan.repo,
        "label": plan.label,
        "note": plan.note,
        "state": plan.state,
        "claim": claim_view(claim) if claim is not None else None,
        "added_by": plan.added_by,
        "created": plan.created_at.isoformat(),
        "updated": latest.isoformat(),
        "idle_days": round(idle, 1),
        "stale": plan.state == "open" and idle >= STALE_DAYS,
        "done": plan.done_at.isoformat() if plan.done_at else None,
        "done_by": plan.done_by,
        **({"items": items} if items is not None else {}),
    }


async def _view_plan(session: AsyncSession, plan: Plan, claim: ResourceLease | None,
                     now: datetime, items: dict[str, int] | None = None) -> dict:
    """One plan, with its items' freshness looked up. The async half of
    :func:`_plan_view`, as :func:`_view_items` is of :func:`_item_view`.

    Every path that renders a single plan goes through here rather than calling
    :func:`_plan_view` itself, so "when did this plan last move" has one answer on
    the write paths and the read paths alike. :func:`_plans_view` does the same
    lookup for a whole list in one query.
    """
    activity = await _plan_activity(session, [plan])
    return _plan_view(plan, claim, now, items=items, active_at=activity.get(plan.id))


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
        # WHO decided that rank — the fact 28 ranked rows could not state (#183).
        # `appended` means nobody did: it went last because that was all the
        # endpoint could do. See `app.models.plan_item.RANK_SOURCES`.
        "rank_source": item.rank_source,
        "placed_for": item.placed_for,
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


def _order_trust(open_views: list[dict]) -> dict:
    """How much of this order anybody actually decided — #183's minimum fix.

    Distinct from ``GET /plan/order`` (#232), and the pair is deliberate: that
    read says what order the RULES imply and never touches the live sequence;
    this says who chose the order already in force. A proposal and a provenance.
    They share one argument — an order whose chosen and unchosen parts are
    indistinguishable gets trusted uniformly, and usually too much.

    ``next`` used to answer rank 1 with no caveat while the human's stated top
    priority sat at rank 20, under a note shouting that its own rank was a lie.
    Every signal that the ranking was untrustworthy was in free text, so no client
    could read it as anything, and the tool's own documentation calls ``next``
    *"the answer, already worked out"*.

    It is worked out from ranks, so it is exactly as good as the ranks are. This
    says how good that is, from the rows themselves rather than from prose:
    ``trusted`` is false while any open item sits where it was merely appended,
    ``from_rank`` is where that starts, and ``by_source`` breaks the list down by
    who chose what. A plan whose every position was placed, submitted or ordered
    is trusted — nobody has to have used the browser for the answer to be honest,
    only somebody has to have chosen.
    """
    by_source: dict[str, int] = {}
    for view in open_views:
        by_source[view["rank_source"]] = by_source.get(view["rank_source"], 0) + 1
    # In the read's own order, which is what makes `from_rank` below meaningful.
    unchosen = [v for v in open_views if v["rank_source"] == "appended"]
    return {
        "trusted": not unchosen,
        "by_source": by_source,
        "unchosen": len(unchosen),
        # Where the order stops meaning anything: the rank of the first item, IN
        # THIS READ'S OWN ORDER, whose position nobody chose. Taken in list order
        # rather than as the smallest rank, because a repo read carries the fleet
        # band after the repo's own items and the two are separate 1..n sequences
        # — `min` over both would name a rank the reader never reaches first.
        # Null when nothing is unchosen, rather than a sentinel to know about.
        "from_rank": unchosen[0]["rank"] if unchosen else None,
        "hint": None if not unchosen else
                "those items are in the order the adds arrived in, because nobody "
                "chose one: pass `after`/`before` to POST /plan/item when you know "
                "where an item belongs, and a human sets the rest at /plan/view",
    }


def _next_caveat(nxt: dict | None, trust: dict, open_n: int) -> str | None:
    """What ``next`` must say when the order it walked is not one anybody decided.

    Returned beside the item rather than instead of it: the answer is still the
    best one available, and an agent that reads nothing else should get it. What
    it must not get is unqualified confidence — this is the issue's sharpest
    complaint, and the minimum fix it asks for even if placement never landed.
    """
    if nxt is None or trust["trusted"]:
        return None
    mine = " including this one," if nxt["rank_source"] == "appended" else ""
    return (
        f"{trust['unchosen']} of {open_n} open items sit where they were "
        f"appended,{mine} from rank {trust['from_rank']} down: nobody chose "
        "those positions. This is the first item that is free, in an order that is "
        "partly just the order things were added — read the notes before you treat "
        "it as a priority."
    )


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
    activity = await _plan_activity(session, plans)
    claims = await _claims_for(
        session, {plan_claim_key(p) for p in plans if p.state == "open"}, now)
    counts = {plan_id: n for plan_id, n in await session.execute(
        select(PlanItem.plan_id, func.count())
        .where(PlanItem.plan_id.in_([p.id for p in plans]), PlanItem.state == "open")
        .group_by(PlanItem.plan_id))}
    return [
        _plan_view(p, claims.get(plan_claim_key(p)) if p.state == "open" else None, now,
                   items={"open": counts.get(p.id, 0)}, active_at=activity.get(p.id))
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


def _place_refused(field: str, token: str, problem: str, hint: str) -> HTTPException:
    """One shape for every refused placement, matching :func:`_dep_refused`.

    Placement fails for the same three reasons a dependency does — no such item,
    not open, not reachable from this scope — so it answers in the same shape, and
    a client that can render one refusal can render both.
    """
    return HTTPException(422, detail={
        "error": f"{field} {token!r}: {problem}", "field": field, "token": token,
        "hint": hint})


async def _resolve_anchor(session: AsyncSession, token: str, repo: str | None,
                          field: str) -> PlanItem:
    """The item a placement is relative to: an item id, or an issue ref like ``#84``.

    The same two spellings :func:`_resolve_dep` takes, for the same reason — an
    agent transcribing a spoken priority has an issue number, not a uuid.

    **The scope is EXACT, and that is the one rule placement adds.** Ranks are
    allocated per scope: a repo's list runs 1..n and the fleet's runs its own
    1..n, so "after the fleet item ranked 3" would name a position in a sequence
    this item is not in. A repo read widens to carry the fleet band along because
    context helps; a write that moves a row cannot, for the same reason
    :class:`ReorderIn` narrows.
    """
    as_uuid = _as_uuid(token)
    if as_uuid is not None:
        item = await session.get(PlanItem, as_uuid)
        if item is None:
            raise _place_refused(field, token, "no such plan item",
                                 "place it next to an item that is in the plan, or "
                                 "leave the position out and it appends")
    else:
        ref = _norm_ref(token)
        if not ref:
            raise _place_refused(field, token, "not an item id or an issue number",
                                 "spell it as an item id or as an issue like '#84'")
        stmt = select(PlanItem).where(
            PlanItem.ref_value == ref, PlanItem.ref_kind == "issue",
            PlanItem.state == "open",
            PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo)
        item = await session.scalar(stmt.order_by(PlanItem.rank).limit(1))
        if item is None:
            raise _place_refused(
                field, token, "nothing open in this scope references that issue",
                "the plan links to issues, so a position is relative to an ITEM — "
                "add the one you mean first, or leave the position out")
    if item.state != "open":
        raise _place_refused(
            field, token, f"that item is {item.state}",
            "order is for open work: finished and dropped items keep no place, so "
            "there is no position beside one")
    if item.repo != repo:
        raise _place_refused(
            field, token,
            f"that item is in the {item.repo or 'fleet'} list, not the "
            f"{repo or 'fleet'} one",
            "ranks are per scope — a repo's list and the fleet's are two sequences, "
            "and a position in one says nothing about the other")
    return item


async def _place_rank(session: AsyncSession, repo: str | None, anchor: PlanItem,
                      *, before: bool) -> int:
    """Make room beside ``anchor`` and return the rank the new item takes.

    Call under :func:`_lock_scope`, like :func:`_next_rank`: this is a
    read-then-write over the same ranks, and two placements computing room from
    one snapshot is the same lost update.

    **OPEN items only, exactly as a reorder renumbers only open ones.** History
    keeps the rank it had — a done row is a record of finished work, not a
    position — so a shift that renumbered it would rewrite the record every time
    somebody placed an item above it.

    ``updated_at`` is deliberately NOT touched on the rows that shift. Staleness
    is "has anybody paid this item any attention", and being renumbered is not
    attention: bumping it would let one placement make a fortnight-old plan read
    as fresh, which is precisely the plan that is believed and wrong.
    """
    rank = anchor.rank if before else anchor.rank + 1
    await session.execute(
        update(PlanItem)
        .where(PlanItem.state == "open", PlanItem.rank >= rank,
               PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo)
        .values(rank=PlanItem.rank + 1))
    return rank


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
                     *, open_only: bool, any_repo: bool = False) -> Plan | None:
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

    ``any_repo`` is the one exception, and only for the id path: an unscoped
    ``GET /plan`` reads EVERY scope, so ``?plan=<id>`` answering 422 unless the
    caller also names the repo made a globally unique id less nameable than the
    read it narrows — the id was the thing that needed no scope. The label path
    keeps its exact scope regardless, because a label is unique per scope and
    widening it would make which "stage 1" you got depend on insertion order.

    **The OPEN plan wins when both exist.** With ``open_only`` off the state
    filter goes, and ``ix_plans_open_label`` is partial on ``state = 'open'`` —
    so one scope may legitimately hold a finished "stage 1" and a live one at
    once, and the finished one was created first. Ordering by ``created_at``
    alone therefore answered ``GET /plan?plan=stage 1`` with the closed plan:
    no items, ``counts.open`` 0, ``next`` null — "nothing to do in stage 1"
    while the live stage 1 was full of work, which is the failure
    :func:`_scope_items` says a filter must never produce. Open first, then most
    recent, because the last "stage 1" is the one somebody naming "stage 1"
    means.
    """
    token = _norm_text(token)
    if not token:
        return None
    as_uuid = _as_uuid(token)
    if as_uuid is not None:
        plan = await session.get(Plan, as_uuid)
        if plan is None or not (any_repo or _in_scope(plan, repo)):
            return None
        return None if open_only and plan.state != "open" else plan
    stmt = select(Plan).where(func.lower(Plan.label) == token.lower())
    if open_only:
        stmt = stmt.where(Plan.state == "open")
    # By label the scope is EXACT rather than widened: two scopes may each hold an
    # open "stage 1" (the unique index is per scope), so widening would make which
    # one you got depend on insertion order.
    stmt = stmt.where(Plan.repo.is_(None) if repo is None else Plan.repo == repo)
    return await session.scalar(
        stmt.order_by(Plan.state != "open", Plan.created_at.desc()).limit(1))


async def _plan_or_422(session: AsyncSession, token: str | None, repo: str | None,
                       *, open_only: bool = True,
                       any_repo: bool = False) -> Plan | None:
    if token is None:
        return None
    plan = await _find_plan(session, token, repo, open_only=open_only,
                            any_repo=any_repo)
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


#: What ``plan`` replaced (#172), and why it is REFUSED rather than ignored.
#: Pydantic drops an unknown body field and FastAPI drops an unknown query
#: parameter, so the old spelling failed three different silent ways: ``POST
#: /plan/item {phase: "stage 1"}`` made a loose item belonging to nothing,
#: ``plan/item/update {phase: ...}`` did nothing at all and answered 200, and
#: ``GET /plan?phase=...`` answered about the whole scope — a broader list than
#: was asked for, which is exactly the shape of "nothing to do here" being wrong.
#: A migration that fails loudly is the cheap kind.
_PHASE_GONE = {
    "error": "`phase` is gone: a plan is a row now, not a string on an item (#172)",
    "hint": "say `plan` instead — the same field on the wire with a row behind it. "
            "An unknown label creates the plan; GET /plans lists them.",
}


def _refuse_phase(value: str | None) -> None:
    if value is not None:
        raise HTTPException(422, detail=_PHASE_GONE)


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
    #: Accepted only so it can be refused — see :data:`_PHASE_GONE`.
    phase: str | None = Field(default=None, max_length=MAX_LABEL)
    #: WHY it sits here. The sentence a human would otherwise repeat to each
    #: agent that asks, which is the half an issue has no field for.
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    #: Item ids, or issue refs (``"#55"``) resolved against the same repo.
    depends_on: list[str] = Field(default_factory=list)
    #: WHERE it goes, as an item id or an issue ref (``"#84"``) in the same scope.
    #: Absent, it appends exactly as it always did. Placing is not reordering —
    #: inserting between ranks 2 and 3 leaves every existing pair's relative order
    #: untouched, so there is no prior decision to overwrite and nothing to thrash
    #: (#183). Permuting what is already there is still :func:`reorder`, and still
    #: human-only.
    after: str | None = Field(default=None, max_length=128)
    before: str | None = Field(default=None, max_length=128)
    #: Whose stated priority this placement transcribes — ``"Rich, 23:00"``.
    #: Refused without a position: on its own it would be one more free-text
    #: priority channel, which is the workaround #183 is about rather than the fix.
    placed_for: str | None = Field(default=None, max_length=MAX_PLACED_FOR)


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
    #: Accepted only so it can be refused — see :data:`_PHASE_GONE`.
    phase: str | None = Field(default=None, max_length=MAX_LABEL)
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
    #: Claim the plan over items somebody else is already holding. Refused by
    #: default and said out loud when used, exactly as ``force`` on an item claim
    #: is: they may genuinely be sharing the work, but then "I know they hold part
    #: of this" is on the record rather than assumed.
    force: bool = False


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
    phase: str | None = Query(default=None,
                              description="gone (#172): a plan is a row now — "
                                          "narrow with `plan` instead"),
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

    **And it says how much that answer is worth.** ``next`` walks rank order, so
    it is exactly as good as the ranks — which for a long time were part real
    sequence and part the order the adds happened to arrive in, with nothing in
    the data telling the two apart (#183). ``ordering`` reports that from the rows
    themselves, and ``next.caveat`` carries it to the agent that reads only the
    headline. That is provenance for the order in force; ``GET /plan/order``
    (#232) is the other question — what order the rules would imply — and neither
    one writes anything.
    """
    _refuse_phase(phase)
    now = _utcnow()
    repo = _norm_scope(repo)
    # An id needs no scope, and only here: an unscoped read covers every scope, so
    # narrowing it by plan id must not be the one thing that cannot reach one.
    scoped = await _plan_or_422(session, plan, repo, open_only=False,
                                any_repo=repo is None and not exact)
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
    trust = _order_trust(open_views)
    nxt = unclaimed[0] if unclaimed else None
    by_state = await _counts_by_state(session, repo, plan_id, exact)
    in_scope = sum(by_state.values()) if include_done else by_state.get("open", 0)
    plans = await _plans_view(session, repo, exact, now, caller,
                              session_id=session_q)
    # The narrowed plan comes OUT of that list rather than being rendered again, so
    # `plan` and the matching row of `plans` cannot disagree about who holds it —
    # rendering it separately gave it `claim: null` while the list showed the claim.
    scoped_view = next((row for row in plans
                        if scoped is not None and row["plan_id"] == str(scoped.id)),
                       None)
    if scoped_view is None and scoped is not None:
        # Narrowed to a plan the list does not carry (a closed one, or — read
        # unscoped by id — another repo's). It still answers with its own view; it
        # is just claimless, which it is.
        scoped_view = await _view_plan(session, scoped, None, now)
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
        # The caveat rides on `next` and not only in `ordering`, because the whole
        # point of `next` is that it is read alone.
        "next": None if nxt is None else
                {**nxt, "caveat": _next_caveat(nxt, trust, len(open_views))},
        # Always present, `trusted: true` and all — a flag that appears only when
        # things are wrong is one a client never learns to look for. Named for the
        # question it answers rather than `ordering`, because `GET /plan/order`
        # answers a different one and two adjacent reads called the same thing is
        # how a word stops meaning anything.
        "order_trust": trust,
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
    """Add an item, appending unless you say where it goes. Placing is not reordering.

    The premise this endpoint has always been documented on — *adding is not
    reordering, so an agent may do it* — was true of adding and false of what the
    code did, because there was no way to add an item without also deciding where
    it went and "last" was hard-coded (#183). That is not the absence of an
    ordering judgement; it is the specific judgement "this is the lowest-priority
    open item", asserted on the caller's behalf and wrong whenever the new item is
    not in fact the least important thing outstanding.

    So a caller may name a neighbour: ``after`` or ``before``, an item id or an
    issue ref in the same scope. It stays agent-permitted because **placing
    changes the relative order of nothing already in the plan** — insert between
    ranks 2 and 3 and every existing pair keeps its existing relationship, so
    there is no prior decision to overwrite and nothing for two agents to thrash.
    Permuting existing items is the contested operation, and that is
    :func:`reorder`, which is human-only and unchanged.

    Absent a position it appends, exactly as before — and says so, in
    ``rank_source``, rather than leaving a reader of 28 ranked rows to work out
    which of them anybody chose.

    A second open item for an issue already in the plan is refused, naming the
    one that is already there — the plan holding two rows about #60 is precisely
    the drift it exists to remove.
    """
    _refuse_phase(body.phase)
    if (body.ref_kind is None) != (_norm_ref(body.ref_value) is None):
        raise HTTPException(422, "a ref needs both kind and value, or neither")
    title = _norm_text(body.title)
    if not title:
        raise HTTPException(422, "a title is what an agent reads in `next`: it cannot be blank")
    ref_value = _norm_ref(body.ref_value)
    repo = _norm_scope(body.repo)
    # Normalised BEFORE the either-or check: `after=""` is no position at all, and
    # refusing `{"after": "", "before": "#84"}` as "two positions" would refuse a
    # request that names one.
    after, before = _norm_text(body.after), _norm_text(body.before)
    if after and before:
        raise HTTPException(422, detail={
            "error": "say `after` or `before`, not both",
            "hint": "a position is one neighbour: two of them are two positions, and "
                    "nothing in the request says they agree"})
    placed_for = _norm_text(body.placed_for)
    if placed_for and not (after or before):
        raise HTTPException(422, detail={
            "error": "`placed_for` records whose priority a PLACEMENT transcribes, "
                     "so it needs `after` or `before`",
            "hint": "without a position it is a priority written into free text, which "
                    "is the workaround this field exists to end (#183) — pass the "
                    "position too, or put the reasoning in `note`"})
    deps = await _resolve_deps(session, body.depends_on, repo, item_id=None)
    plan = await _ensure_plan(session, body.plan, repo, author)
    # Held to the commit: both `_next_rank` and `_place_rank` are read-then-write
    # over the same ranks, and two adds in one scope working from one snapshot is a
    # lost update with no unique index behind it to notice — two items at the same
    # position, ordered thereafter by whichever happened to be created first.
    await _lock_scope(session, repo)
    if after or before:
        anchor = await _resolve_anchor(session, before or after, repo,
                                       "before" if before else "after")
        rank = await _place_rank(session, repo, anchor, before=bool(before))
    else:
        rank = await _next_rank(session, repo)
    item = PlanItem(
        repo=repo, title=title, ref_kind=body.ref_kind, ref_value=ref_value,
        plan_id=plan.id if plan is not None else None,
        note=_norm_text(body.note), depends_on=deps,
        added_by=author, rank=rank,
        rank_source="placed" if (after or before) else "appended",
        placed_for=placed_for,
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
    # `mine` on a write path too: without it the author's OWN plan claim came back
    # as `covered_by`, so an agent adding to the plan it holds was told the plan
    # was somebody else's — the page renders that verbatim.
    return (await _view_items(session, [item], _utcnow(), mine=author))[0]


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
    #
    # What the re-check hands back is THIS request's claim and not a moment more:
    # `acquire` may have renewed one the caller already held, and taking that away
    # would leave an agent that legitimately had the item holding nothing at all —
    # see :func:`_hand_back`, and `claim_kept` in the refusals below.
    await session.refresh(item)
    if item.state != "open":
        kept = _hand_back(claim, renewed, now)
        await session.commit()
        raise HTTPException(409, detail={
            "error": f"that item became {item.state} while you were claiming it",
            "item_id": str(item.id), "claim_kept": kept,
            "hint": "re-read the plan: it moved under you"})
    if not body.force:
        raced = await _covering_claim(session, item, holder, body.session, now)
        if raced is not None:
            kept = _hand_back(claim, renewed, now)
            await session.commit()
            raise HTTPException(409, detail={
                "error": "somebody took the whole plan while you were claiming this item",
                "item_id": str(item.id), "covered_by": raced, "claim_kept": kept,
                "hint": "re-read the plan: it moved under you. Talk to them, or pass "
                        "force=true to take one item out of it deliberately"})
    # One instant per request: `now` stamps the claim, the row and the view it
    # renders, so a single logical moment is not three slightly different ones.
    item.updated_at = now
    await session.commit()
    view = (await _view_items(session, [item], now, mine=holder,
                              session_id=body.session))[0]
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
    return {**(await _view_items(session, [item], now, mine=holder,
                                 session_id=body.session))[0], "released": released}


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
    view = (await _view_items(session, [item], now, mine=holder,
                              session_id=body.session))[0]
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
    holder: str = Depends(identify),
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
    # No `session` on this body, so coverage is answered by machine — the coarser
    # honest answer `_covered_by` documents, rather than reporting the caller's own
    # plan claim as somebody else's.
    return (await _view_items(session, [item], now, mine=holder))[0]


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
    _refuse_phase(body.phase)
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
    return {**(await _view_items(session, [item], now, mine=editor))[0],
            "edited_by": editor}


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
    listed = {i.id for i in ordered}
    now = _utcnow()
    for rank, item in enumerate([*ordered, *rest], start=1):
        # `ordered` is the human's sequence and `rest` is what the page did not
        # know about, so only the listed items become `ordered`: an item that
        # arrived after the page loaded was carried along, not decided on, and
        # marking it would make `GET /plan` claim a human had chosen a position
        # they never saw (#183).
        source = "ordered" if item.id in listed else item.rank_source
        if item.rank != rank or item.rank_source != source:
            item.rank, item.rank_source, item.updated_at = rank, source, now
    await session.commit()
    return {
        "repo": repo, "reordered": len(ordered), "by": editor,
        "appended": [str(i.id) for i in rest],
        # Says who is asking, as every write path now does. A human is never a claim
        # holder, so here it changes nothing — the uniformity is the point: the
        # defect was the one call site that did not say, and rendered the caller's
        # own plan claim as somebody else's cover.
        "items": await _view_items(
            session, await _scope_items(session, repo, exact=True, include_done=False),
            now, mine=editor),
    }


# --- suggested order (#232's deterministic slice) ---------------------------
#
# Rule 3 above says only a human reorders the plan. That is a rule about who may
# *write* the sequence, and it left the fleet with nowhere to put the other
# thing: a machine-readable answer to "what order do the facts imply?". #232 asks
# for an agent that owns the order and is told what its last orders cost — and
# the part of that needing no agent, no claim and no judgement is this: run the
# deterministic rules, publish `suggested_order` beside `active_order`, and write
# every proposal down with its evidence so that when the agent does exist it
# inherits a record instead of an oracle.
#
# Three properties make it safe to ship ahead of the agent:
#
#   * it never mutates the live sequence, so it cannot thrash the plan and does
#     not need #183 settled first;
#   * `suggested_order` is shaped exactly like `POST /plan/reorder`'s `order`, so
#     applying it is one human call and applying it is the ONLY way it takes
#     effect;
#   * it says which placements the rules derived and which they could not, so the
#     half a model would be asked about is a field rather than an inference.


def _ev_key(view: dict) -> tuple[str, int] | None:
    """The ``(repo, pr)`` a plan item's ref names, or None if it names no PR."""
    ref = view.get("ref")
    if not ref or ref.get("kind") != "pr" or not view.get("repo"):
        return None
    try:
        return (view["repo"].lower(), int(ref["value"]))
    except (TypeError, ValueError):
        return None


async def _pr_evidence(session: AsyncSession,
                       items: list[PlanItem]) -> tuple[dict[tuple[str, int], dict], dict[str, str]]:
    """The newest panel run for each PR the plan references, then classified.

    **Select first, classify second**, and that ordering is #101's whole finding
    rather than a preference: a rival PR answered for by its newest *file-bearing*
    run, and then by its newest *OPEN-state* run, were the same defect twice —
    "any predicate placed before the selection resurrects a stale run". So the
    query below carries exactly two predicates, both of them about identity
    (which repo, which PRs) and none about the state of a run, and every reading
    of that run — merged, drafted, red, unanswered findings — is taken afterwards
    in code where it is visible that one run answered for one PR.

    ``DISTINCT ON`` is Postgres-only and that is settled: this service has never
    been able to run on anything else (migration ``0001`` creates a plpgsql
    function, ``LISTEN``/``NOTIFY`` *is* the SSE leg, and the README lists
    Postgres under *Architecture (decided)*).

    Repository names are folded to lower case on both sides. GitHub repos are
    case-insensitive, ``_norm_scope`` lower-cases the plan's copy for exactly that
    reason, and ``review_runs.repo`` is stored as the panel sent it — so a run
    recorded as ``PrisonBlues/quarterback`` would otherwise leave its PR looking
    like one the board had never seen, which is the silent-absence failure #101
    is filed about wearing a different hat.

    Returns the evidence keyed by ``(repo, pr)``, and the items whose ref could
    not be resolved to one — reported, never dropped.
    """
    wanted: dict[tuple[str, int], list[str]] = {}
    problems: dict[str, str] = {}
    for item in items:
        if item.ref_kind != "pr" or not item.ref_value:
            continue
        if not item.repo:
            problems[str(item.id)] = "names a PR but no repo, so no run can be found for it"
            continue
        try:
            pr = int(item.ref_value)
        except ValueError:
            problems[str(item.id)] = f"ref {item.ref_value!r} is not a PR number"
            continue
        wanted.setdefault((item.repo.lower(), pr), []).append(str(item.id))
    if not wanted:
        return {}, problems

    repo_key = func.lower(ReviewRun.repo)
    runs = list(await session.scalars(
        select(ReviewRun)
        .where(tuple_(repo_key, ReviewRun.pr).in_(list(wanted)))
        .distinct(repo_key, ReviewRun.pr)
        .order_by(repo_key, ReviewRun.pr, ReviewRun.ts.desc(), ReviewRun.id.desc())
    ))
    # Confirmed findings on those runs, and which of them somebody has recorded an
    # outcome for. All four outcomes count as answered, `deferred` included: it
    # says the work was moved to an issue, which is a decision, and treating it as
    # outstanding would keep an item at the head of the plan for a finding
    # somebody has already dealt with. NO outcome row is what counts as open —
    # nobody has said, which is neither fixed nor refuted (v2.37).
    confirmed: dict[int, set[str]] = {}
    if runs:
        for run_id, key in await session.execute(
            select(ReviewFinding.run_id, ReviewFinding.finding_key)
            .where(ReviewFinding.run_id.in_([r.id for r in runs]),
                   ReviewFinding.verdict == "confirmed")
        ):
            confirmed.setdefault(run_id, set()).add(key)
    answered: set[tuple[str, int, str]] = set()
    if confirmed:
        for repo, pr, key in await session.execute(
            select(ReviewFindingOutcome.repo, ReviewFindingOutcome.pr,
                   ReviewFindingOutcome.finding_key)
            .where(tuple_(func.lower(ReviewFindingOutcome.repo),
                          ReviewFindingOutcome.pr).in_(list(wanted)))
        ):
            answered.add((repo.lower(), pr, key))

    evidence: dict[tuple[str, int], dict] = {}
    for run in runs:
        pair = (run.repo.lower(), run.pr)
        keys = confirmed.get(run.id, set())
        evidence[pair] = {
            "run_id": run.id,
            "as_of": run.ts.isoformat(),
            "round": run.round,
            "head_sha": run.head_sha,
            "pr_state": run.pr_state,
            "draft": run.is_draft,
            "ci": run.ci_status,
            "confirmed": len(keys),
            # Only CONFIRMED findings count as work, matching every other number
            # this board publishes about a round — and the count that was left out
            # rides along beside it, because a rule that quietly ignores a
            # category is a rule nobody can argue with. A finding no judge ruled
            # on is not evidence of anything yet (v2.37), and a round recorded
            # with `judged: false` is all unjudged.
            "unjudged": run.n_unjudged,
            "outstanding_findings": sum(1 for k in keys if (*pair, k) not in answered),
        }
    return evidence, problems


def _candidates(views: list[dict], items: dict[str, PlanItem],
                evidence: dict[tuple[str, int], dict], now: datetime) -> list[Candidate]:
    """The plan, as facts the rules read — in rank order, which IS the active order.

    ``depends_on`` is the item's OPEN blockers rather than its raw ``depends_on``:
    a dependency on a finished item is an edge with no effect (``_item_view``
    already filters the same way for the same reason), and ordering behind it
    would hold work back forever. ``blocked`` is the same list as a boolean,
    which is what carries an out-of-scope blocker into the rules — the walk can
    only honour an edge whose other end is being ordered, and "it waits on
    something open" is the whole of what can be said about the rest.

    ``collides_with`` is left empty and ``overlap_known`` false at the call site:
    changed-file overlap lives in ``review_run_files`` (#82) but the collision
    query over it is #101 and still open. The rules treat it as a refinement, so
    its absence widens the ambiguous set and changes nothing else.

    The idle age is computed here from the row rather than taken from the view's
    ``idle_days``, which is ROUNDED for display — ``round(idle, 1)``. An item at
    13.96 days renders as 14.0, so the staleness rule would have called it stale
    while ``_item_view``'s own ``stale`` flag, computed from the unrounded age,
    still said no: two answers to one question, disagreeing for an hour a
    fortnight, and the order moving on the display precision of a number
    (Codex, review pass five).
    """
    out = []
    for v in views:
        ev = evidence.get(_ev_key(v)) or {}
        out.append(Candidate(
            key=v["item_id"],
            depends_on=tuple(b["item_id"] for b in v["blocked_by"]),
            blocked=bool(v["blocked_by"]),
            pr_state=ev.get("pr_state"),
            draft=ev.get("draft"),
            ci=ev.get("ci"),
            outstanding_findings=ev.get("outstanding_findings"),
            idle_days=(now - items[v["item_id"]].updated_at).total_seconds() / 86400,
            # WHICH run said so. Stored on the placement and folded into the
            # digest, so a reading can be traced to its source afterwards and a
            # re-panel is a new proposal rather than a duplicate of the old one.
            evidence=_provenance(ev) or None,
        ))
    return out


#: The provenance half of a PR's evidence: which run, when, at what commit, and
#: the two finding counts that say what the round adjudicated. Split out from the
#: readings themselves (``pr_state``/``draft``/``ci``/``outstanding_findings``,
#: which ride the placement's ``inputs.readings``) so that neither is stored twice
#: and free to disagree with its copy.
_PROVENANCE_FIELDS = ("run_id", "as_of", "round", "head_sha", "confirmed", "unjudged")


def _provenance(ev: dict) -> dict:
    return {k: ev[k] for k in _PROVENANCE_FIELDS if k in ev}


def _unknown(views: list[dict], evidence: dict[tuple[str, int], dict],
             problems: dict[str, str], now: datetime) -> list[dict]:
    """What the rules could not read, named rather than left absent.

    An order computed from partial evidence and published as if it were complete
    is the failure #101 describes from the other end — a rival "read by a caller
    as answered, and disjoint". So every input the gather could not get is listed
    with the items it affects, including the ordinary cases: most of a plan
    references issues, not PRs, and has no review state at all.
    """
    out: list[dict] = [{
        "input": "overlap",
        "reason": "changed-file overlap is not computed yet — the data is in "
                  "review_run_files (#82), the collision query over it is #101 and open",
        "consequence": "two items touching the same files are not separated on that "
                       "account, so they stay in the ambiguous set",
        "items": None,
    }]
    no_ref, never, stale = [], [], []
    for v in views:
        key = _ev_key(v)
        if key is None:
            if str(v["item_id"]) not in problems:
                no_ref.append(v["item_id"])
            continue
        ev = evidence.get(key)
        if ev is None:
            never.append(v["item_id"])
            continue
        age = (now - datetime.fromisoformat(ev["as_of"])).total_seconds() / 86400
        if age >= EVIDENCE_STALE_DAYS:
            stale.append({"item_id": v["item_id"], "as_of": ev["as_of"], "age_days": round(age, 1)})
    if no_ref:
        out.append({
            "input": "review_state",
            "reason": "the item references an issue, or nothing — there is no PR to read "
                      "CI, draft state or findings from",
            "consequence": "placed on dependency edges, blockers and staleness alone",
            "items": no_ref,
        })
    if never:
        out.append({
            "input": "review_state",
            "reason": "the board has never recorded a panel run for this PR; it knows only "
                      "the PRs it has panelled, so this is not evidence the PR is fine",
            "consequence": "placed on dependency edges, blockers and staleness alone",
            "items": never,
        })
    if stale:
        out.append({
            "input": "review_state",
            "reason": f"the newest panel run is at least {EVIDENCE_STALE_DAYS} days old; it "
                      "is still the best evidence there is, and it is a snapshot",
            "consequence": "CI status and PR state may have moved since",
            "items": stale,
        })
    if problems:
        out.append({
            "input": "ref",
            "reason": "the item's ref could not be resolved to a PR",
            "consequence": "placed on dependency edges, blockers and staleness alone",
            "items": [{"item_id": k, "problem": v} for k, v in sorted(problems.items())],
        })
    return out


class Computed(NamedTuple):
    """One run of the rules over one scope, and everything a caller needs about it."""

    views: list[dict]
    evidence: dict[tuple[str, int], dict]
    result: Ordering
    unknown: list[dict]


async def _compute_order(session: AsyncSession, repo: str | None, now: datetime) -> Computed:
    """Read the scope, gather the evidence, run the rules. No writes, ever.

    **The scope is exact, and there is no widening option**, which is the one place
    this differs from ``GET /plan``. A read widens because context helps; an order
    cannot, because ``rank`` is allocated per scope — a repo's list is 1..n and the
    fleet's is its own 1..n — so a widened read is two sequences interleaved by the
    scope band rather than one sequence anybody could put into force. And putting
    it into force is the point: ``suggested_order`` is only useful if it can be
    handed to ``POST /plan/reorder``, which takes one exact scope.
    """
    items = await _scope_items(session, repo, exact=True, include_done=False)
    if len(items) > MAX_ORDER_ENTRIES:
        raise HTTPException(422, detail={
            "error": f"{len(items)} open items in this scope is past the "
                     f"{MAX_ORDER_ENTRIES}-item cap for an order",
            "repo": repo, "open_items": len(items),
            "hint": "nothing is truncated: an order is not pageable, and a partial one would "
                    "read as an order about the whole scope. A plan this size is the thing to "
                    "fix — its four rules keep it to tens of rows"})
    views = await _view_items(session, items, now)
    evidence, problems = await _pr_evidence(session, items)
    result = suggest_order(
        _candidates(views, {str(i.id): i for i in items}, evidence, now),
        stale_days=STALE_DAYS)
    return Computed(views, evidence, result, _unknown(views, evidence, problems, now))


def _order_entry(placement: dict, view: dict, evidence: dict) -> dict:
    """One placement, plus the handles a reader needs to act on it.

    Deliberately NOT the item's phase or plan grouping: the order is over a whole
    scope, nothing here reads that field, and #172 is replacing it mid-flight —
    so carrying it would be a coupling to a column being renamed under this code
    for the sake of a field no caller of this endpoint needs. `GET /plan` has it.
    """
    return {
        **placement,
        "title": view["title"],
        "ref": view["ref"],
        "rank": view["rank"],
        # WHO chose that rank (#183). A move is a different proposition depending
        # on whether the position it replaces was decided by a human or was merely
        # where `plan_add` put the row — and this endpoint's whole argument is
        # that a reader must be able to tell derived from judged.
        "rank_source": view["rank_source"],
        # Evidence, never a rule. A claim expires passively, so ordering on it
        # would make the sequence flap on a TTL — and `next` already skips a
        # claimed item, which is the behaviour that question actually wants.
        "claim": view["claim"],
        # The run's identity and its finding counts are on the placement
        # (``evidence``); this is the rest of what that run said, for a reader
        # rather than for a rule.
        "run": evidence.get(_ev_key(view)),
    }


def _order_view(repo: str | None, now: datetime, c: Computed) -> dict:
    """The proposal as the API publishes it: the orders, the evidence, the remainder.

    ``entries`` is each placement plus the handles a reader needs to act on it —
    title, ref, rank, who holds it. Those are context for a human and are
    deliberately NOT what gets stored: a recorded proposal keeps item ids and the
    rules' own inputs, and the title is recovered by joining the plan row that
    still exists. Rule 1 of this module is that the plan never restates what lives
    elsewhere, and a ledger of copies is the same mistake one indirection out.
    """
    body = c.result.as_dict()
    placements = body.pop("placements")
    by_key = {v["item_id"]: v for v in c.views}
    return {
        "repo": repo,
        "generated": now.isoformat(),
        # Said out loud because it is a rule, not a default — see _compute_order.
        "scope": "exact",
        **body,
        "entries": [_order_entry(p, by_key[p["key"]], c.evidence) for p in placements],
        "unknown": c.unknown,
        # What it would take to make this the live order, stated in the payload
        # rather than left to a reader: one human call, and nothing else in the
        # board reads `suggested_order` back. #232's non-privileged-writer rule.
        "apply": {
            "endpoint": "POST /plan/reorder",
            "human_only": True,
            "body": {"repo": repo, "order": list(c.result.suggested_order)},
        },
    }


@router.get("/plan/order")
async def read_order(
    repo: str | None = Query(default=None,
                             description="the scope, EXACTLY — null is the fleet-wide list"),
    _: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What order the rules would put this scope in, and how much of it they decided.

    Read-only and side-effect free: it computes, it does not record. Recording is
    ``POST /plan/order-proposal``, and the split is deliberate — a proposal is
    something somebody makes, while this is a question anybody may ask as often as
    they like without adding a row to a ledger.

    The answer separates *derived* from *ambiguous*, per item, in ``basis``:

    * ``constraint`` — a dependency edge or an open blocker put it here. Facts this
      database owns, and removals of a contradiction rather than assertions (#183).
    * ``preference`` — a graded rule did: a merged PR sinks, red CI or unanswered
      confirmed findings rise, a long-untouched item rises. Deterministic, but
      policy, and mostly read off a panel run's snapshot.
    * ``ambiguous`` — nothing separated it from a peer. It kept the position it
      already had, and **this is the remainder** a model would be asked about.
    * ``unopposed`` / ``unresolved`` — nothing to compare it against, and a
      dependency cycle the rules refuse to repair.

    ``counts.derived`` against ``counts.ambiguous`` and ``counts.interchangeable``
    is the figure to read first: it says how much of this order is a fact.

    No placement is chosen by a coin: ties fall back to the order in force, so if no
    rule fires anywhere the suggestion is the sequence you already have. Applying a
    rule to a pair with something between them does shift that something — no
    sequence inverts one pair and no other — and every crossing no rule ordered
    carries a ``displaced`` reason naming what went past, at both ends, rather
    than moving unexplained.
    """
    now = _utcnow()
    repo = _norm_scope(repo)
    return _order_view(repo, now, await _compute_order(session, repo, now))


class OrderProposalIn(BaseModel):
    #: The scope, exactly as ``ReorderIn`` means it.
    repo: str | None = None
    session: str | None = Field(default=None, max_length=MAX_SESSION)
    #: Record it even when an identical proposal is already the newest one for
    #: this scope. Off by default: a cron floor that runs whether or not anything
    #: is dirty (#232) would otherwise fill the ledger with copies, and a ledger
    #: of copies cannot show when the answer changed.
    force: bool = False


def _proposal_view(row: OrderProposal, *, full: bool) -> dict:
    """One recorded proposal. ``full`` carries the per-item evidence.

    The list read omits ``placements`` for the reason ``_run_view`` omits
    ``changed_files``: it is the unbounded half of the row, and a page of twenty
    would be a page of file-sized JSON. The counts derived from it still ride the
    list, because "how much of that proposal was derived" is exactly what a reader
    scanning the ledger is looking for.
    """
    placements = list(row.placements or [])
    # Seeded with every basis at zero, so this read and a freshly computed one have
    # the SAME shape. A `counts` that carried only the bases a particular row
    # happened to contain would make every consumer test for a key on one endpoint
    # and index it on the other — one fact with two shapes, which is how a tool
    # ends up guessing which it is looking at.
    by_basis: dict[str, int] = dict.fromkeys(BASES, 0)
    for p in placements:
        key = p.get("basis", "?")
        by_basis[key] = by_basis.get(key, 0) + 1
    moves = moves_between(row.active_order or [], row.suggested_order or [])
    view = {
        "id": row.id,
        "ts": row.ts.isoformat(),
        "repo": row.repo,
        "proposed_by": row.proposed_by,
        "session": row.session,
        "source": row.source,
        "rules_version": row.rules_version,
        "inputs_digest": row.inputs_digest,
        "overlap_known": row.overlap_known,
        "active_order": list(row.active_order or []),
        "suggested_order": list(row.suggested_order or []),
        # Both derived on read from the two orders above, never stored: a column
        # would be free to disagree with the columns it came from.
        "changed": list(row.active_order or []) != list(row.suggested_order or []),
        "moves": list(moves),
        "counts": {
            **by_basis,
            "entries": len(placements),
            "derived": by_basis.get("constraint", 0) + by_basis.get("preference", 0),
            "moved": len(moves),
            "interchangeable": sum(1 for p in placements if p.get("ambiguous_with")),
            "ambiguous_groups": len(row.ambiguous or []),
        },
        "ambiguous": list(row.ambiguous or []),
        "cycles": list(row.cycles or []),
        "unknown": list(row.unknown or []),
    }
    if full:
        view["placements"] = placements
    return view


@router.post("/plan/order-proposal", status_code=201)
async def record_order_proposal(
    body: OrderProposalIn,
    response: Response,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record what the rules propose for this scope, with its evidence.

    **The caller does not supply an order.** The board computes it, from the plan
    and the panel runs it already holds, and stores what its own rules produced —
    so a row here is always a claim the rules made and never one an agent asserted
    and labelled deterministic. An agent's opinion belongs on the board, addressed
    to whoever is deciding (``board_post(type='finding', to=…)``), which is #232's
    "suggestions in, order out" and needs no endpoint.

    This is the prediction side of #232's ledger, and the reason to build it before
    the agent: a planner told only its own recent choices "will defend its prior
    order", so what it has to be given is *order proposed → what happened → the
    delta*. The first term has to have been written down while it was still a
    prediction. Nothing here reads it back, and nothing acts on it: putting an
    order into force is ``POST /plan/reorder``, and only a human may.

    Identical to the newest proposal for the scope? Nothing is written and
    ``recorded`` comes back false with the existing row — a cron floor that runs
    dirty or not would otherwise bury the moment the answer changed under a
    thousand copies of it. ``force`` records anyway.
    """
    now = _utcnow()
    repo = _norm_scope(body.repo)
    # The SAME per-scope lock `reorder` takes, and taken for the reason it is there:
    # what follows reads the newest proposal, decides against it, and inserts — so
    # without it two concurrent runs both see no predecessor and both write, which
    # is the duplicate the digest comparison exists to prevent. Cheap here, because
    # a proposal is not a hot path.
    #
    # It is NOT #232's `kind=work, key=plan-order:<repo>` claim and does not stand
    # in for it: that claim serialises planner RUNS across machines and is the
    # planner's to take. This is a transaction lock inside one request.
    await _lock_scope(session, repo)
    computed = await _compute_order(session, repo, now)
    result = computed.result
    previous = await session.scalar(
        select(OrderProposal)
        .where(OrderProposal.repo.is_(None) if repo is None else OrderProposal.repo == repo)
        .order_by(OrderProposal.id.desc()).limit(1))
    # The digest covers the facts the rules read AND the sequence they read them
    # against, so an unchanged digest under unchanged rules is the same proposal.
    # `suggested_order` is compared too: it is derived from those two, so a
    # disagreement means the rules changed under a version that did not say so,
    # and recording it is how that becomes visible instead of deduplicated away.
    same = (
        previous is not None
        and not body.force
        and previous.rules_version == result.rules_version
        and previous.inputs_digest == result.inputs_digest
        and list(previous.suggested_order or []) == list(result.suggested_order)
    )
    if same:
        # 200, not the declared 201: nothing was created. `/review/outcomes` draws
        # the same distinction between a row it wrote and one it found already
        # there, and a caller polling on a cron floor should be able to see which
        # happened from the status line alone.
        response.status_code = 200
        return _recorded(previous, computed, repo, now, recorded=False,
                         reason="identical to the newest proposal for this scope")

    stored = result.as_dict()
    row = OrderProposal(
        repo=repo,
        proposed_by=author,
        session=clean_session(body.session),
        # Set here and never taken from the body: "these were the rules" is a
        # statement only the thing that ran them may make.
        source="deterministic",
        rules_version=result.rules_version,
        inputs_digest=result.inputs_digest,
        overlap_known=result.overlap_known,
        active_order=list(result.active_order),
        suggested_order=list(result.suggested_order),
        placements=stored["placements"],
        ambiguous=stored["ambiguous"],
        cycles=stored["cycles"],
        unknown=computed.unknown,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _recorded(row, computed, repo, now, recorded=True, reason=None)


def _recorded(row: OrderProposal, c: Computed, repo: str | None, now: datetime, *,
              recorded: bool, reason: str | None) -> dict:
    """The stored row, plus the context a caller cannot get from the row alone.

    Not the whole of ``_order_view`` splatted on top: that would answer with two
    copies of the same orders and counts, one from the computation and one from
    the row, free to be read as if they were different facts. The row IS the
    answer; ``entries`` adds the titles and refs a stored proposal deliberately
    does not duplicate, and ``apply`` names the one call that puts it in force.

    **The top level describes now; ``proposal`` describes when it was written.**
    That distinction is why ``unknown`` is here as well as on the row, and it is
    load-bearing in one case: evidence ages. A run six days old carries no
    staleness caveat and the same run at eight days does, while every fact the
    rules read is unchanged — so the proposal is correctly deduplicated (a
    threshold crossed by a clock is not a new proposal) and the caveat is
    correctly current. Publishing only the row's copy would have answered a
    caller asking today with the caveats of the day it was recorded, which is the
    one reading a staleness warning must never be given.
    """
    by_key = {v["item_id"]: v for v in c.views}
    view = _proposal_view(row, full=True)
    out = {
        "recorded": recorded,
        "proposal": view,
        "entries": [_order_entry(p, by_key[p["key"]], c.evidence)
                    for p in view["placements"] if p["key"] in by_key],
        # As of now, not as of the row — see the docstring.
        "unknown": c.unknown,
        "generated": now.isoformat(),
        "apply": {
            "endpoint": "POST /plan/reorder",
            "human_only": True,
            "body": {"repo": repo, "order": list(row.suggested_order or [])},
        },
    }
    if reason:
        out["reason"] = reason
    return out


@router.get("/plan/order-proposals")
async def list_order_proposals(
    repo: str | None = Query(default=None, description="only this scope"),
    exact: bool = Query(default=False,
                        description="treat repo=null as the fleet-wide scope ONLY, rather "
                                    "than as every scope"),
    limit: int = Query(default=20, ge=1, le=100),
    _: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The last N proposals, newest first — what the rules said, and when.

    This is what #232's planner gets handed on wake, minus the half that does not
    exist yet: the outcome of each. That absence is deliberate and is stated
    rather than stubbed, because "did they land in this order?" needs a merge
    order, a rebase count and a staleness reading this release does not gather,
    and a null column invites the question to be answered by whoever is looking —
    the self-grading loop #40 and #77 both refuse.

    What it *is* good for today: reading whether an answer changed, and why. Two
    consecutive rows with the same ``inputs_digest`` describe the same world; a
    changed digest with an unchanged ``suggested_order`` says the world moved and
    the order did not.
    """
    stmt = select(OrderProposal)
    if repo is not None:
        stmt = stmt.where(OrderProposal.repo == _norm_scope(repo))
    elif exact:
        stmt = stmt.where(OrderProposal.repo.is_(None))
    rows = list(await session.scalars(stmt.order_by(OrderProposal.id.desc()).limit(limit)))
    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())) or 0
    return {
        "repo": _norm_scope(repo),
        "scope": "exact" if repo is not None or exact else "all",
        "proposals": [_proposal_view(r, full=False) for r in rows],
        "count": len(rows),
        "truncated": len(rows) < total,
        "outcomes": "not recorded yet — the other half of #232's ledger",
    }


@router.get("/plan/order-proposal/{proposal_id}")
async def get_order_proposal(
    proposal_id: int,
    _: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One recorded proposal, with the per-item evidence the list read omits."""
    row = await session.get(OrderProposal, proposal_id)
    if row is None:
        raise HTTPException(404, "order proposal not found")
    return _proposal_view(row, full=True)
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
    (the default) takes the plan in the same call, because the surveying agent
    wrote it and the gap between writing and holding is the gap.

    **The claim is taken BEFORE the write, not after.** ``acquire`` commits, so it
    cannot be part of this transaction either way — and taking it afterwards
    reopens the same window one notch later: the plan and its items are committed
    and readable, and for as long as the second transaction takes there is nothing
    holding them. Taking it first cannot collide (the key is this request's own
    fresh id) and cannot fail the caller after persisting anything; if the write
    then fails, the claim is handed straight back.

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

    # The cap applies to the MERGED list, and it is cheapest to say so here: only
    # the `outside` tokens reach `_resolve_deps`, so without this a submitted row
    # could carry 32 external edges plus 63 `@n` ones — refused on the same row by
    # `POST /plan/item/depends`. Before anything is written, because a submission
    # is all-or-nothing and "refuse it" is only available while nothing is.
    for position, item in enumerate(body.items, start=1):
        if len(item.depends_on) > MAX_DEPS:
            raise _too_many_deps(position)

    batch = _batch_deps(body.items)

    existing = await _find_plan(session, label, repo, open_only=True)
    if existing is not None:
        raise _label_taken(existing)

    # The plan's id is minted HERE rather than at flush, because the claim below has
    # to be taken before the plan is readable.
    plan = Plan(id=uuid.uuid4(), repo=repo, label=label, note=_norm_text(body.note),
                added_by=author)
    claimed, claim_id = None, None
    if body.claim:
        # **Before the write, not after.** `acquire` commits — that is where its
        # atomicity comes from — so a claim taken afterwards is a second
        # transaction, and the gap between the two is a plan that is readable and
        # unheld: precisely the window this endpoint exists to close, moved from
        # between two items to between the plan and its claim. It also meant a
        # failing claim answered with an error *after* the plan and every item had
        # been persisted. Nobody can be holding `plan:<a fresh uuid>`, so this
        # cannot conflict; what it buys is the ordering.
        claim, _ = await acquire(session, ClaimRequest(
            kind=CLAIM_KIND, key=plan_claim_key(plan), holder=author, ttl=body.ttl,
            sess=body.session,
            note=_norm_text(body.note_on_claim) or f"planning: {label}",
            now=now, session_owned=True))
        claimed, claim_id = claim_view(claim), claim.id

    try:
        # Held to the commit: every item's rank comes off `_next_rank`, and a
        # submission is the case where that read-then-insert happens `len(items)`
        # times in a row.
        await _lock_scope(session, repo)
        session.add(plan)
        rank = await _next_rank(session, repo)
        plan_items: list[PlanItem] = []
        for position, (item, title, ref) in enumerate(
                zip(body.items, titles, refs, strict=True)):
            plan_items.append(PlanItem(
                repo=repo, title=title, ref_kind=item.ref_kind, ref_value=ref,
                note=_norm_text(item.note), added_by=author, rank=rank + position,
                # The submitter wrote this list in this order, so every item after
                # the first sits where somebody put it — `submitted` rather than
                # `appended`, and not `ordered` either, because a submitted plan is
                # a proposal and #183's `ordering` report says so.
                #
                # **The FIRST item is an append, and marking it otherwise would
                # hide the one seam a submission really does leave.** Where the
                # block itself goes is decided by `_next_rank` and by nobody:
                # submit two plans into one scope and the second sits behind the
                # first for no reason anyone stated. Calling all of them
                # `submitted` reported that scope as fully trusted, which is
                # exactly the "17 chosen, 11 by arrival, nothing telling them
                # apart" this issue is about — one boundary instead of eleven, but
                # the same lie. So each submission contributes exactly one
                # unchosen position, and `from_rank` names the seam.
                rank_source="appended" if position == 0 else "submitted",
                depends_on=[]))
        for row in plan_items:
            session.add(row)
        # Ids first: an `@2` edge needs the id of a row that has not been assigned
        # one yet, and a plan whose edges are written in a second transaction is the
        # incremental plan this endpoint replaces.
        await session.flush()
        for row in plan_items:
            row.plan_id = plan.id
        # External edges resolve against the database, batch edges against this
        # submission, and the two merge in the order the caller wrote them.
        for position, (row, item) in enumerate(zip(plan_items, body.items, strict=True)):
            outside = [t for t in item.depends_on if not str(t).startswith(_BATCH_REF)]
            resolved = await _resolve_deps(session, outside, repo, item_id=row.id)
            inside = [str(plan_items[i].id) for i in batch[position]]
            row.depends_on = list(dict.fromkeys([*resolved, *inside]))
        await session.commit()
    except IntegrityError as e:
        # ONE handler for the whole write, because the flush that trips an index is
        # not always the explicit one: `_next_rank` is a SELECT, so autoflush inserts
        # the plan row *there*, and a label race therefore failed before the flush
        # this used to guard — arriving as a 500 rather than as any answer about the
        # plan at all.
        await session.rollback()
        await _undo_claim(session, claim_id, now)
        if not is_unique_violation(e):
            raise
        raise await _submit_conflict(session, repo, label, body.items, refs) from None
    except Exception:
        # The claim above is ordering, not a record. A claim left standing over a
        # plan that was never written is a key nobody can release, held by an agent
        # that was told its submission failed — so it goes back before the refusal
        # the caller actually sees.
        await _undo_claim(session, claim_id, now)
        raise

    await session.refresh(plan)
    return {
        **await _view_plan(session, plan, None, now, items={"open": len(plan_items)}),
        "claim": claimed,
        "items": await _view_items(session, list(plan_items), now, mine=author,
                                   session_id=body.session),
    }


def _label_taken(plan: Plan) -> HTTPException:
    """409: a plan by that name is already open in this scope.

    Named once because two paths arrive at it. The pre-check reads the label
    before the write; the LOSER of a race between two submissions of one label
    meets ``ix_plans_open_label`` on the flush instead — `_lock_scope` is taken
    after the pre-check, so both pass it — and used to be answered by
    :func:`_submit_conflict`, which looks only for colliding item refs, found
    none, and said "that submission collided with an existing row" with
    ``clashes: []`` and advice to drop lines that were fine. The caller was told
    to edit its plan and never told the name was taken. One refusal, so the timing
    cannot change the answer.
    """
    return HTTPException(409, detail={
        "error": f"a plan called {plan.label!r} is already open here",
        "plan_id": str(plan.id), "repo": plan.repo,
        "hint": "add to it with POST /plan/item (plan=<label>), or finish it "
                "first — submitting over it would be two plans with one name"})


async def _undo_claim(session: AsyncSession, claim_id: uuid.UUID | None,
                      now: datetime) -> None:
    """Hand back a claim taken for a write that then failed.

    **It rolls the session back first, and that is load-bearing**: handing the
    claim back is itself a COMMIT, and this is called on the failure paths — so a
    session still carrying the refused submission would land exactly the rows the
    422 says were not written. A refused ring of dependencies committed half a plan
    the first time this was written without the rollback.

    An UPDATE by id rather than a touch on the instance, because after that
    rollback the ORM object is expired and reading it would re-emit the statement
    that just failed — the same reason :func:`_ref_taken` takes plain values.
    """
    await session.rollback()
    if claim_id is None:
        return
    await session.execute(
        update(ResourceLease)
        .where(ResourceLease.id == claim_id, ResourceLease.released_at.is_(None))
        .values(released_at=now))
    await session.commit()


async def _submit_conflict(session: AsyncSession, repo: str | None, label: str,
                           asked: list[SubmitItemIn], refs: list[str | None],
                           ) -> HTTPException:
    """409 naming what the submission collided with: the label, or which ref.

    A submission is all-or-nothing, so the caller needs to know *which* line to
    change — "something in there collides" would make it bisect its own plan.

    The LABEL is looked for first, and is why this takes one: the plan row and the
    items go in on the same flush, so the unique index that fired may have been
    ``ix_plans_open_label`` rather than ``ix_plan_items_open_ref``, and a caller
    whose only problem is the name must not be sent to edit lines that are
    correct. Re-read rather than decided from the constraint name, because the
    answer wanted is the same 409 the pre-check gives and that is a row, not a
    string.
    """
    taken = await _find_plan(session, label, repo, open_only=True)
    if taken is not None:
        return _label_taken(taken)
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


async def _held_items(session: AsyncSession, plan: Plan, holder: str,
                      session_id: str | None, now: datetime) -> list[dict]:
    """The open items of this plan somebody ELSE is holding, with who and until when.

    The other direction of the coverage rule, and it was missing. ``claim_item``
    refuses an item inside a plan another agent holds; nothing refused the plan to
    an agent when another already truthfully held items inside it — so "all of
    this is mine" could be said over work that demonstrably was not, and both
    claims stayed live. That is overlapping ownership, which is the one outcome
    both grains exist to prevent, and it does not matter which of the two arrived
    first.

    Ownership is :func:`_is_mine`, as everywhere else on this router: the items you
    are holding yourself are no obstacle to claiming the plan they are in — that is
    the ordinary way a plan is worked through.
    """
    items = list(await session.scalars(
        select(PlanItem).where(PlanItem.plan_id == plan.id, PlanItem.state == "open")))
    if not items:
        return []
    claims = await _claims_for(session, {claim_key(i) for i in items}, now)
    held = []
    for item in items:
        claim = claims.get(claim_key(item))
        if claim is None or _is_mine(claim, holder, session_id):
            continue
        held.append({"item_id": str(item.id), "title": item.title,
                     "ref": item.ref_value, "holder": claim.holder,
                     "session": claim.session, "note": claim.note,
                     "expires": claim.expires_at.isoformat()})
    return held


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

    **Coarse does not mean it wins.** An item another agent already holds is
    refused (see :func:`_held_items`): a plan claim taken over it would leave two
    live claims on one piece of work, each of whose holders is right — the exact
    outcome the plan grain exists to prevent, in the direction ``claim_item``
    already guarded and this one did not. ``force`` is the way to say it anyway,
    and then it is in the record.
    """
    now = _utcnow()
    plan = await _get_plan(session, body.plan_id)
    if plan.state != "open":
        raise HTTPException(409, detail={
            "error": f"that plan is {plan.state}", "plan_id": str(plan.id)})
    held = await _held_items(session, plan, holder, body.session, now)
    if held and not body.force:
        raise HTTPException(409, detail={
            "error": "items in that plan are held by somebody else",
            "plan_id": str(plan.id), "held_items": held,
            "hint": "claiming the plan says all of it is yours, and they truthfully "
                    "hold part of it — talk to them (their sessions are above), take "
                    "the items you need one at a time, or pass force=true to claim "
                    "the plan over theirs deliberately"})
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
        kept = _hand_back(claim, renewed, now)
        await session.commit()
        raise HTTPException(409, detail={
            "error": f"that plan became {plan.state} while you were claiming it",
            "plan_id": str(plan.id), "claim_kept": kept,
            "hint": "re-read the plans: it moved under you"})
    if not body.force:
        # And the same window on the other check: an item claim landing between
        # `_held_items` and here leaves both grains live, which is the state this
        # endpoint's own refusal is about.
        raced = await _held_items(session, plan, holder, body.session, now)
        if raced:
            kept = _hand_back(claim, renewed, now)
            await session.commit()
            raise HTTPException(409, detail={
                "error": "somebody claimed an item inside that plan while you were "
                         "claiming the plan",
                "plan_id": str(plan.id), "held_items": raced, "claim_kept": kept,
                "hint": "re-read the plan: it moved under you. Talk to them, or pass "
                        "force=true to claim the plan over their items deliberately"})
    plan.updated_at = now
    await session.commit()
    return {**await _view_plan(session, plan, claim, now), "claimed": True,
            "renewed": renewed, "claim_id": str(claim.id), "forced": bool(held)}


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
    return {**await _view_plan(session, plan, None, now), "released": released}


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
    return {**await _view_plan(session, plan, None, now),
            "claim_left": None if mine or claim is None else claim_view(claim),
            "items_left": [str(i.id) for i in left]}
