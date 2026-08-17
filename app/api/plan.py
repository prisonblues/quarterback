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
from app.db import get_session
from app.models.plan_item import PlanItem
from app.models.resource_lease import ResourceLease

router = APIRouter(tags=["plan"])

#: Plan claims and hand-taken work claims are the SAME claims. `kind='work'`,
#: `key='<repo>#<issue>'` is the convention agents converged on by hand before
#: this table existed, and matching it is what makes the two views agree.
CLAIM_KIND = "work"

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
