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
from typing import Literal, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text, tuple_
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
from app.db import get_session
from app.models.order_proposal import OrderProposal
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease
from app.models.review import ReviewFinding, ReviewFindingOutcome, ReviewRun
from app.ordering import BASES, Candidate, Ordering, moves_between, suggest_order

router = APIRouter(tags=["plan"])

#: Plan claims and hand-taken work claims are the SAME claims. `kind='work'`,
#: `key='<repo>#<issue>'` is the convention agents converged on by hand before
#: this table existed, and matching it is what makes the two views agree.
CLAIM_KIND = "work"

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

    An issue-backed item uses the key agents already take by hand
    (``prisonblues/quarterback#142``), so the plan sees claims it never
    mediated. Anything else — a PR ref, or a plan item with no ref at all — is
    keyed by item id: ``<repo>#<n>`` cannot be shared between an issue and a PR
    numbered the same, and a keyless item still needs a key of its own.
    """
    if item.repo and item.ref_kind == "issue" and item.ref_value:
        return f"{item.repo}#{item.ref_value}"
    return f"plan:{item.id}"


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
    return repo.strip().lower() or None


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
            "hint": "an item waiting on thirty others is a phase, not an item"})
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


async def _claims_for(session: AsyncSession, items: list[PlanItem],
                      now: datetime) -> dict[str, ResourceLease]:
    """The live claim on each item's key, in one query rather than one per item."""
    keys = {claim_key(i) for i in items}
    if not keys:
        return {}
    rows = await session.scalars(
        select(ResourceLease).where(
            ResourceLease.kind == CLAIM_KIND, ResourceLease.key.in_(keys),
            ResourceLease.released_at.is_(None), ResourceLease.expires_at > now)
    )
    return {r.key: r for r in rows}


def _item_view(item: PlanItem, claim: ResourceLease | None,
               blockers: list[PlanItem], now: datetime) -> dict:
    idle = (now - item.updated_at).total_seconds() / 86400
    return {
        "item_id": str(item.id),
        "repo": item.repo,
        "title": item.title,
        "ref": {"kind": item.ref_kind, "value": item.ref_value} if item.ref_kind else None,
        "phase": item.phase,
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


async def _view_items(session: AsyncSession, items: list[PlanItem], now: datetime) -> list[dict]:
    """Render items with their live claims and their open blockers.

    A claim attaches to an OPEN item only. Claims are keyed by ``repo#issue``, so
    an issue that was finished and later re-added shares its key with the item
    that replaced it — and a history read then showed the new item's live claim
    sitting on the old done row, which reads as "this finished work is currently
    being worked on by somebody".
    """
    claims = await _claims_for(session, [i for i in items if i.state == "open"], now)
    known = {str(i.id): i for i in items}
    wanted = {d for i in items for d in (i.depends_on or [])} - set(known)
    known |= await _load(session, wanted)
    return [
        _item_view(
            item, claims.get(claim_key(item)) if item.state == "open" else None,
            [known[d] for d in (item.depends_on or [])
             if d in known and known[d].state == "open"],
            now,
        )
        for item in items
    ]


async def _scope_items(session: AsyncSession, repo: str | None, exact: bool,
                       include_done: bool, limit: int | None = None,
                       phase: str | None = None) -> list[PlanItem]:
    stmt = select(PlanItem)
    if phase is not None:
        # Filtered in SQL, ahead of the LIMIT. Filtering the page afterwards
        # dropped every matching item past the first `limit` rows — and with it
        # `next`, which would read as "nothing to do in this phase".
        stmt = stmt.where(PlanItem.phase == phase)
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


async def _counts_by_state(session: AsyncSession, repo: str | None, phase: str | None,
                           exact: bool) -> dict[str, int]:
    """How many items in this scope are in each state — over the WHOLE scope.

    An aggregate rather than a count of the page: history is the unbounded half
    of this table, and `done`/`dropped` counted off a `limit`-truncated read
    reported "3 finished" for a repo with three hundred.
    """
    stmt = select(PlanItem.state, func.count()).group_by(PlanItem.state)
    if phase is not None:
        stmt = stmt.where(PlanItem.phase == phase)
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


class ItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    repo: str | None = Field(default=None, max_length=256)
    ref_kind: Literal["issue", "pr"] | None = None
    #: ``"60"`` or ``"#60"`` — normalised, so one issue cannot become two items.
    ref_value: str | None = Field(default=None, max_length=64)
    phase: str | None = Field(default=None, max_length=64)
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
    phase: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    state: Literal["open", "dropped"] | None = None


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
    phase: str | None = Query(default=None, description="only this phase"),
    exact: bool = Query(default=False,
                        description="this scope ONLY — do not widen a repo read to the "
                                    "fleet-wide items (and, with no repo, the fleet list "
                                    "by itself)"),
    limit: int = Query(default=200, ge=1, le=1000,
                       description="most items to return, from the TOP of the order"),
    _: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What is next, in order, with who has what — the one call an agent makes cold.

    ``next`` is the answer to the actual question: the first item that is open,
    unclaimed and unblocked. An agent that reads nothing else still gets a
    truthful answer, and one that reads the list sees why the items above it
    were skipped (held by somebody, or waiting on something).
    """
    now = _utcnow()
    repo = _norm_scope(repo)
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
                                    phase=phase)
    open_views = await _view_items(session, open_items, now)
    if include_done:
        views = await _view_items(
            session,
            await _scope_items(session, repo, exact=exact, include_done=True,
                               limit=limit, phase=phase),
            now)
    else:
        views = open_views[:limit]
    unclaimed = [v for v in open_views if not v["claim"] and not v["blocked_by"]]
    by_state = await _counts_by_state(session, repo, phase, exact)
    in_scope = sum(by_state.values()) if include_done else by_state.get("open", 0)
    return {
        "repo": repo,
        "exact": exact,
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
    # Held to the commit: `_next_rank` is a read-then-insert, and two adds in one
    # scope both reading the same maximum is a lost update with no unique index
    # behind it to notice — two items at the same position, ordered thereafter by
    # whichever happened to be created first.
    await _lock_scope(session, repo)
    item = PlanItem(
        repo=repo, title=title, ref_kind=body.ref_kind, ref_value=ref_value,
        phase=_norm_text(body.phase), note=_norm_text(body.note), depends_on=deps,
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
    claim, renewed = await acquire(session, ClaimRequest(
        kind=CLAIM_KIND, key=claim_key(item), holder=holder, ttl=body.ttl,
        sess=body.session, note=_claim_note(item, body.note, blockers), now=now,
        session_owned=True))
    # The state check above and the claim are two statements, and an item can be
    # finished or dropped between them. Nothing can lock across `acquire` (it
    # commits — that is where its atomicity comes from), so the check is made
    # again afterwards and the claim handed straight back if it lost: a claim on
    # a dropped item is a claim nobody can act on and nobody can see.
    await session.refresh(item)
    if item.state != "open":
        claim.released_at = now
        await session.commit()
        raise HTTPException(409, detail={
            "error": f"that item became {item.state} while you were claiming it",
            "item_id": str(item.id), "hint": "re-read the plan: it moved under you"})
    # One instant per request: `now` stamps the claim, the row and the view it
    # renders, so a single logical moment is not three slightly different ones.
    item.updated_at = now
    await session.commit()
    view = (await _view_items(session, [item], now))[0]
    return {**view, "claimed": True, "renewed": renewed, "claim_id": str(claim.id),
            "forced": bool(blockers)}


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
    """Retitle, rephase, re-reason, or drop an item. Human-only, like reordering.

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
    if body.phase is not None:
        item.phase = _norm_text(body.phase)
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
