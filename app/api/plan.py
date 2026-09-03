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
   the shared intent it exists to be, so ``POST /plan/reorder`` never takes an
   order an agent *decided*. What it does take, since #478, is one a person asked
   for: :func:`app.auth.delegated` accepts a person, or an agent presenting its own
   machine's credential — and the row then records ``derived`` rather than
   ``ordered``, so the two are never confused. Deciding stays a person's. Choosing where a NEW item *enters* alters the relative order of
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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    ColumnElement,
    Text,
    case,
    delete,
    false,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    lapse_hint,
    live_claim,
    may_mutate,
)
from app.auth import author, delegated, human, identify, reader
from app.claimkey import WORK, BadRef, canonical_repo, derive, work_ref
from app.db import get_session
from app.identity import HUMAN, is_human, same_machine
from app.models.blocker import Blocker
from app.models.order_proposal import OrderProposal
from app.models.plan import Plan
from app.models.plan_item import PlanItem
from app.models.plan_reconcile import PlanReconcile
from app.models.plan_reconcile_pass import PlanReconcilePass
from app.models.plan_scope import PlanScope
from app.models.post import Post
from app.models.resource_lease import ResourceLease
from app.models.review import ReviewFinding, ReviewFindingOutcome, ReviewRun
from app.needs_human import label_for
from app.ordering import BASES, Candidate, Ordering, moves_between, suggest_order
from app.review_queue import (
    EXEMPTABLE_REF_KIND,
    MarkerInReason,
    exempting,
    grant_line,
    granted_exemption,
    request_line,
    requested_exemption,
    safe_reason,
    strip_exemption_lines,
)
from app.scope import (
    PROJECT_SIGIL,
    SCOPE_SHAPE,
    canonical_project,
    canonical_scope,
    is_project,
    project_name,
)

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
#: What separates a completion receipt from the reasoning it is appended to. A
#: constant because the rule is written twice — once in Python for a plan's note
#: (:func:`_completion_note`) and once in SQL for an item's
#: (:func:`_completion_note_sql`) — and this is the part that would drift.
_DONE_SEP = "\n— done: "
#: A plan label is a handle an agent says out loud, not a description. The old
#: ``phase`` column was bounded at 64 on the wire and this keeps that.
MAX_LABEL = 64
#: Provenance for a placed item — "Rich, 2026-08-17 23:00". An attribution, not a
#: justification: the reasoning still goes in ``note``, and a field long enough to
#: hold an argument would collect one.
MAX_PLACED_FOR = 120
#: A scope, on the wire. Long enough for the sigil plus the 64 characters
#: :data:`app.scope.PROJECT_NAME_RE` allows, and for any ``owner/name`` GitHub can
#: issue. The `repo` fields stay at 256 for compatibility with what is already
#: stored; this bounds the one field that only ever carries a scope name.
MAX_SCOPE = len(PROJECT_SIGIL) + 64
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


async def _norm_scope(session: AsyncSession, repo: str | None) -> str | None:
    """``""`` is not a repo — it is a third scope nothing agrees on.

    The unique index keys on ``COALESCE(repo, '')``, so an empty-string repo
    collides with the fleet items; :func:`claim_key` reads it as falsy and keys
    the item by id; and ``_next_rank`` ranks it as a scope of its own. One
    normalisation at the edge, and the three cannot disagree.

    Lower-cased for the same reason, one level up: GitHub repository names are
    case-insensitive, so ``Acme/Repo`` and ``acme/repo`` are one repo everywhere
    except here, where they would pass the uniqueness index as two open items
    with two claim keys — "one open item per issue" defeated by a shift key.

    **A scope is not always a repo** (#323). ``project:65lowther`` is a scope with
    no forge behind it, and it passes here — but only if a *person* has declared
    it, which is the check that needs this session. See :mod:`app.scope` for the
    two gates and why neither can be dropped: the sigil is what stops a mistyped
    repo becoming a scope, and the registry is what stops a mistyped scope
    becoming one. Everything without the sigil goes to
    :func:`~app.claimkey.canonical_repo` exactly as before, and a bare
    ``quarterback`` still meets #148's refusal.
    """
    if repo is None:
        return None
    if not repo.strip():
        return None
    if is_project(repo):
        return await _declared_scope(session, repo)
    try:
        return canonical_repo(repo)
    except BadRef:
        # Refused rather than stored, because from #172 onward the repo is half of
        # a derived claim key: a bare `quarterback` beside a
        # `prisonblues/quarterback` is the two-spellings defect back again, one
        # level down, and it would key the same issue two ways. The lower-casing
        # this used to do was necessary and not sufficient — it made `Acme/Repo`
        # and `acme/repo` agree and left `repo` and `acme/repo` disagreeing.
        #
        # #323 changed the MESSAGE and not one thing about the refusal: `65lowther`
        # is still refused as a repo, and the added sentence says the other
        # namespace exists rather than opening a way into it from here.
        raise HTTPException(422, SCOPE_SHAPE) from None


async def _declared_scope(session: AsyncSession, repo: str) -> str:
    """A ``project:`` scope, if a person has declared it. 422 naming the ones they have.

    **The 422 is the whole anti-typo gate**, so it carries what a caller needs to
    get it right rather than only what it got wrong: an agent that mistyped a live
    scope sees the real one in the list and fixes itself, and an agent that meant a
    new one learns it is not its call to make. The list is read only on the way to
    that refusal — the path that succeeds asks one indexed question and is done.

    The shape is checked first and separately. ``project:`` with nothing after it
    is a malformed scope and not an undeclared one, and answering "no scope called
    ``project:`` is declared" would send a caller looking for a row it should never
    create.
    """
    try:
        wanted = canonical_scope(repo)
    except BadRef as e:
        raise HTTPException(422, str(e)) from None
    if await session.scalar(select(PlanScope.name).where(PlanScope.name == wanted)):
        return wanted
    known = list(await session.scalars(select(PlanScope.name).order_by(PlanScope.name)))
    raise HTTPException(422, detail={
        "error": f"no scope called {wanted!r} has been declared",
        "scope": wanted,
        "declared": known,
        "hint": "a project scope is a scope with no repo behind it, and a PERSON "
                "declares it (POST /plan/scope, from the board in a browser). An "
                "agent cannot: a scope invented from a typo is a second name for "
                "work that already has one, and nothing would reconcile the two "
                "lists. If you meant a repo, spell it `owner/name`.",
    })


def _refuse_forge_ref(repo: str | None, ref_kind: str | None,
                      position: int | None = None) -> None:
    """An ``issue`` or ``pr`` ref needs a forge to point into. A project scope has none.

    ``ref_kind`` is what makes an item *link rather than restate* — the plan's
    first rule — and the two kinds it takes today are both GitHub's. A repo scope
    supplies the other half of that link; ``project:65lowther`` supplies nothing to
    resolve ``#7`` against, and an item carrying one would be a link to a page that
    does not exist, rendered as a URL on the board page and chased by
    ``qb-reconcile`` every quarter of an hour.

    Refused rather than ignored, for :data:`_PHASE_GONE`'s reason: a field silently
    dropped is a caller believing it wrote something. And refused **on the ref kind
    rather than on "does this scope have issues"**, which is the axis #327 is about
    — when a git-native ref kind exists (a branch, a commit, a worktree), it needs
    no forge and belongs in a project scope, and only this one function has to
    learn that.
    """
    if ref_kind is None or not is_project(repo):
        return
    raise HTTPException(422, detail={
        "error": f"{f'item {position}: ' if position is not None else ''}"
                 f"a {ref_kind} ref needs a repo, and {repo!r} is a project scope",
        **({"item": position} if position is not None else {}),
        "scope": repo,
        "hint": "a project scope has no forge behind it, so there is nothing for "
                f"{ref_kind} to name. Put the item in the repo scope if the work "
                "really is an issue or a PR, or drop the ref and let the title and "
                "note carry it — which is what every item in a project scope does.",
    })


#: The longest reason an exemption request or grant may carry into an item's
#: ``note``. Short on purpose: the note is a human's reasoning about the plan and
#: this is one line inside it, not the argument. The argument goes in the board
#: post, which has room.
MAX_EXEMPT_REASON = 500

#: How long the same exemption question goes unrepeated on the board. The same
#: bound ``harness/loops/needs_human.py`` puts on :func:`announce`, and for the
#: same reason: the note already refuses a second request while one is pending,
#: but withdrawing and asking again would slip past that and cost a person one
#: notification per loop iteration. An escalation queue nobody can trust to be
#: quiet is one nobody reads.
EXEMPT_ANNOUNCE_WINDOW = timedelta(hours=1)

#: #279's class for "which of these, or whether at all". An exemption from review
#: is a policy call about a specific PR, so it is a ``decision`` and not a taste
#: or an environment question. Spelled once, here, and validated at import by the
#: module that owns the vocabulary.
EXEMPT_DECISION_CLASS = "decision"


def _refuse_agent_exemption(ref_kind: str | None, note: str | None,
                            *, position: int | None = None) -> None:
    """Refuse an exemption an agent wrote for the PR the exemption is about (#335).

    #273's queue lets a PR leave the review backlog three ways: merged, closed,
    or exempted by an open plan item. The drainer was carefully forbidden to
    decide the third for itself — and then the marker was put in ``note``, on an
    endpoint any agent may call, which handed the worker the authority the
    drainer had been denied. Not by defeating a check: by using the documented
    API exactly as intended.

    That is the self-approval argument #85 and #86 each settled about
    ``require_human_triage``, and #78 about ``judge_model``, one level further
    out. Those two govern whether work *starts*; this governs whether work is
    *inspected before it lands*, which is the sharper instance. #85's sentence
    transfers unaltered: **the label that authorises work has to come from
    someone who is not the worker.**

    So the marker is a human write, for the reasoning :func:`app.auth.human`
    already gives. Two paths can put it on an open item and **neither will take it
    from an agent**: :func:`exempt_item`'s grant half, which downgrades an agent's
    ``grant`` to a request; and ``POST /plan/item/update``, which since #478 runs
    on :func:`app.auth.delegated` and calls this function for any caller that is
    not a person. That second path is why this guard is not merely belt-and-braces
    — widening that endpoint's gate without it reopened #335 through one of the two
    doors this argument depends on, and a delegated agent exempted its own PR.

    **The refusal is not a dead end**, which is the other half of #335 and the
    reason this is not a bare 403: an agent may still *propose* an exemption at
    ``POST /plan/item/exempt``, and the proposal is durable, attributed and
    announced to a person. A refusal with nowhere for the request to go is one
    agents route around.

    Scoped to :data:`app.review_queue.EXEMPTABLE_REF_KIND` because that is the
    only ref kind the queue reads a marker off — an item about an *issue* whose
    note happens to discuss ``review: exempt`` exempts nothing, and refusing it
    would make writing about this feature harder than using it.
    """
    if ref_kind != EXEMPTABLE_REF_KIND or not exempting(note):
        return
    raise HTTPException(403, detail={
        "error": f"{f'item {position}: ' if position is not None else ''}"
                 "exempting a PR from review is a human write: an agent may not "
                 "write the `review: exempt` marker on the plan item for a PR",
        **({"item": position} if position is not None else {}),
        "hint": "the authorisation to skip a check cannot come from the party the "
                "check is on (#85, #86, #78, #335). Ask for it instead: "
                "POST /plan/item/exempt {item_id|repo+pr, reason} records the "
                "request on the item, announces it to a person on the board, and "
                "leaves the PR in the queue until they grant it.",
        "propose": "POST /plan/item/exempt",
    })


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


def _review_view(item: PlanItem) -> dict | None:
    """This item's exemption state, for a reader that should not parse a note.

    The marker is a token inside free text — that is what makes it cheap, and it
    is also what would have every consumer writing its own regex over ``note``
    within a fortnight. One derivation, published beside the note it came from,
    so the plan page and the review queue cannot come to different views of the
    same row.
    """
    if item.ref_kind != EXEMPTABLE_REF_KIND:
        return None
    granted, pending = granted_exemption(item.note), requested_exemption(item.note)
    return {
        "exempt": exempting(item.note),
        "granted_by": None if granted is None else granted.by,
        # The BOOLEAN is the fact a reader acts on, and it is not the same as
        # having an author: a marker somebody typed by hand carries neither name
        # nor reason, and a page keying its chip off `requested_by` would render
        # nothing at all for the one request nobody can attribute. That is the
        # direction that loses information, so the flag is published separately.
        "requested": pending is not None,
        "requested_by": None if pending is None else pending.by,
        "requested_reason": None if pending is None else pending.reason,
    }


async def _reconcile_for(session: AsyncSession,
                         items: list[PlanItem]) -> dict[tuple[str, str, str], PlanReconcile]:
    """What the last pass said about these items' refs, keyed the way a ref is addressed.

    Only refs, and only the ones on this page: an item with no ref cannot be
    reconciled against anything, and a fleet read would otherwise load every row
    the table has to answer about twenty.
    """
    keys = {(i.repo, i.ref_kind, i.ref_value)
            for i in items if i.repo and i.ref_kind and i.ref_value}
    if not keys:
        return {}
    rows = await session.scalars(
        select(PlanReconcile).where(
            tuple_(PlanReconcile.repo, PlanReconcile.ref_kind,
                   PlanReconcile.ref_value).in_(keys)))
    return {(r.repo, r.ref_kind, r.ref_value): r for r in rows}


def _reconcile_view(row: PlanReconcile | None, now: datetime) -> dict | None:
    """What a reader gets: the condition, the pass's own words, and how long.

    ``days`` is the one thing no single pass can report — it holds no history, so
    "flagged for two days" only exists because `first_seen` survives the passes
    that followed. It is also the number that decides how a reader should feel
    about it: minutes old is a race, two days old is a plan nobody is maintaining.
    """
    if row is None:
        return None
    return {
        "condition": row.condition,
        "said": row.said,
        "since": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
        "days": round((now - row.first_seen).total_seconds() / 86400, 1),
        "reported_by": row.reported_by,
    }


def _item_view(item: PlanItem, claim: ResourceLease | None,
               blockers: list[PlanItem], now: datetime,
               plan: Plan | None = None, plan_claim: ResourceLease | None = None,
               mine: str | None = None, session_id: str | None = None,
               reconciled: PlanReconcile | None = None,
               waiting_on_a_human: Sequence[Blocker] = ()) -> dict:
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
        # What the note says about review, read once here rather than by every
        # consumer greping prose. `null` on anything that is not a PR item,
        # because the marker means nothing there (#335).
        "review": _review_view(item),
        #: What the last reconcile pass found about this item's ref, or null when
        #: it found nothing and when no pass has run (#463). NOT a state: the item
        #: is still whatever somebody set it to, and `next` still returns it.
        "reconcile": _reconcile_view(reconciled, now),
        "depends_on": list(item.depends_on or []),
        # Only OPEN dependencies block: a dropped one will never be done, and
        # waiting on it forever would be the plan quietly lying about "next".
        # TWO kinds, reported apart because the remedy is different: one waits on
        # work, the other waits on a person. `blocked_by` keeps its shape and its
        # meaning for the item-to-item edges — a reader that only knows about
        # those is not broken by this — and the human kind gets a field of its
        # own rather than being folded in as a fake item (#328).
        "blocked_by": [{"item_id": str(b.id), "title": b.title,
                        "ref": b.ref_value, "repo": b.repo} for b in blockers],
        "waiting_on_a_human": [
            {"blocker_id": str(w.id), "class": w.kind, "question": w.question,
             "owner": w.owner, "raised_by": w.raised_by,
             "raised": w.raised_at.isoformat() if w.raised_at else None,
             "idle_days": round((now - w.raised_at).total_seconds() / 86400, 1)
             if w.raised_at else None}
            for w in (waiting_on_a_human or ())],
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
    ``by_source`` breaks the list down by who chose what, and ``first_unchosen``
    points at one row rather than declaring a boundary. A plan whose every
    position was placed, submitted, picked up or ordered is trusted — nobody has to have used
    the browser for the answer to be honest, only somebody has to have chosen.

    **``first_unchosen`` is an item and not a rank**, and the difference is a
    claim this refused to make. A bare rank invites "everything from here down is
    unchosen", which is false the moment a placed item follows an appended one —
    and it does not even name one position, because a repo read carries the fleet
    band along and the two are separate 1..n sequences, so "rank 3" is two rows.
    So it carries the item's id, its rank and its scope, and asserts about that
    row alone; ``unchosen`` is how many there are, and ``by_source`` is where they
    are concentrated.
    """
    by_source: dict[str, int] = {}
    for view in open_views:
        by_source[view["rank_source"]] = by_source.get(view["rank_source"], 0) + 1
    # In the read's own order, so `first_unchosen` is the first one a reader
    # walking this list actually meets.
    unchosen = [v for v in open_views if v["rank_source"] == "appended"]
    # `derived` rows are counted beside `unchosen`, never inside it, and they do
    # NOT flip `trusted` (#478). A delegated reorder was asked for by a person and
    # computed from facts, so it is not a position nobody chose — and the
    # `picked-up` migration already settled the general case: counting a new
    # source as untrusted makes the plan read as less trustworthy "for the sole
    # reason that agents were working", swamping the signal that the human's
    # ordering has gaps with the signal that the fleet is busy. Weaker than
    # `ordered` and much stronger than `appended` is a count, not a boolean.
    derived = [v for v in open_views if v["rank_source"] == "derived"]
    return {
        "trusted": not unchosen,
        "by_source": by_source,
        "unchosen": len(unchosen),
        "derived": len(derived),
        "derived_hint": None if not derived else
                        "an agent applied that order on somebody's instruction, "
                        "computed from `GET /plan/order`'s rules. It is a real "
                        "decision and it is not a person's own sequence — read "
                        "the board for who asked and when.",
        # One row, named exactly: the first item in this read whose position
        # nobody chose. Null when there is none, rather than a sentinel a client
        # has to know about.
        "first_unchosen": None if not unchosen else {
            "item_id": unchosen[0]["item_id"], "rank": unchosen[0]["rank"],
            "repo": unchosen[0]["repo"]},
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

    Three separate things can qualify the answer and all are said when all apply:
    the last reconcile pass found this item's work already finished (#463), the row
    at the top is work somebody abandoned (#427), and the order was partly nobody's.

    **In that order, and it is not arbitrary.** A reader who stops after one
    sentence should have been told the thing that changes what they do next, and
    "this is already done" changes it completely, while "the ranks below rank 21
    are unchosen" changes it hardly at all.
    """
    if nxt is None:
        return None
    parts = [p for p in (_reconciled_caveat(nxt), _abandoned_caveat(nxt),
                         _unchosen_caveat(nxt, trust, open_n))
             if p]
    return " ".join(parts) or None


def _reconciled_caveat(nxt: dict) -> str | None:
    """Said when the last pass found this item's work already finished, or unlike itself.

    The failure this exists for, with its own times on it: at 10:40Z a plan read
    answered ``next: #449``; #449 had been closed as completed at 07:33Z; and the
    reconcile pass seven minutes earlier had said so, naming it by rank. Both facts
    were on this board and no reader saw them together — so the caveat that DID
    fire was ``_abandoned_caveat``, telling the reader to go and ask the agent who
    put it down whether it had been finished. The board already knew.

    **It does not skip the item, and nothing here marks it done.** That is the
    distinction `qb-reconcile` draws and this keeps: `done_candidate` on a closed
    issue is a record that has been overtaken, but `dropped_candidate` is a
    judgement about abandoned work, and a reader has to make it. So both are said
    and neither is acted on. The plan is still what somebody set it to.

    ``days`` is quoted for the same reason it is stored. One of the three items
    this was written for had been closed for over two days while the pass named it
    every fifteen minutes, and "since Sunday" is what tells a reader they are
    looking at an unmaintained list rather than a race they lost by seconds.
    """
    found = nxt.get("reconcile")
    if not found:
        return None
    days = found["days"]
    lately = ("in the last hour" if days < 0.05 else
              f"for {days:g} day{'' if days == 1 else 's'}")
    said = found.get("said")
    quoted = f" — {said}" if said else ""
    if found["condition"] == "done_candidate":
        return (
            f"THE LAST RECONCILE PASS SAYS THIS IS ALREADY FINISHED{quoted}. It has "
            f"said so {lately}, and the plan has not caught up — the item is open "
            f"here because nobody called `plan_done`, not because the work is "
            f"outstanding. Check the issue before you start; if it is closed, this "
            f"is a plan to tidy rather than work to do."
        )
    if found["condition"] == "dropped_candidate":
        return (
            f"The last reconcile pass says the work behind this was abandoned rather "
            f"than completed{quoted} ({lately}). Whether that means the item should "
            f"be dropped is a decision nobody has made — it is deliberately not made "
            f"for you here, and it is worth making before starting."
        )
    return (
        f"The last reconcile pass flagged this item as `{found['condition']}`"
        f"{quoted} ({lately}). It is still offered as next; read what the pass "
        f"found before treating the row as accurate."
    )


def _abandoned_caveat(nxt: dict) -> str | None:
    """Said when ``next`` is a pickup whose claim is gone. Never a silent promotion.

    A ``picked-up`` row sits at rank 1 because an agent claimed the work, and that
    is a true thing to say while the claim is live — it is what makes the promotion
    harmless, since ``next`` skips claimed items and walks past it to the same free
    item it would have found before.

    **Being ``next`` at all is therefore proof the justification has expired**, and
    that is why this needs no claim lookup: ``next`` is the first OPEN, UNCLAIMED,
    unblocked item, so a ``picked-up`` row can only reach it once nobody holds it.
    The rank then outranks a human's ordered list on the strength of a claim that
    no longer exists, and the plan went on reporting ``trusted: true`` — which is
    correct about the position (an action chose it) and misleading about the answer.

    Not demoted, and not hidden. Work an agent started and put down is a genuinely
    good thing to pick up next, and it is the first thing a human scanning the plan
    should see. It just may not arrive as an unqualified recommendation.
    """
    if nxt["rank_source"] != "picked-up":
        return None
    return (
        "This is at the top because an agent claimed it, and that claim is gone — "
        "so it is work somebody started and put down, not a position anybody "
        "ranked above the rest. That makes it a good pick and not a stated "
        "priority: read its note, and check with whoever held it before you "
        "assume it was abandoned rather than finished."
    )


def _unchosen_caveat(nxt: dict, trust: dict, open_n: int) -> str | None:
    """The original caveat: how much of the order anybody actually decided."""
    if trust["trusted"]:
        return None
    first = trust["first_unchosen"]
    # Says where the unchosen positions START and never that everything after
    # them is one of them — a placed item can perfectly well follow an appended
    # one, and a caveat that overstates its case is read past like any other.
    mine = " this one among them," if nxt["rank_source"] == "appended" else ""
    return (
        f"{trust['unchosen']} of {open_n} open items sit where they were "
        f"appended and nobody chose those positions —{mine} the first at rank "
        f"{first['rank']} of the {first['repo'] or 'fleet'} list. This is the "
        "first free item in rank order, and that order is partly just the order "
        "things were added: read the notes before you treat it as a priority."
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
    reconciled = await _reconcile_for(session, items)
    open_items = [i for i in items if i.state == "open"]
    claims = await _claims_for(
        session,
        {claim_key(i) for i in open_items}
        | {plan_claim_key(p) for p in plans.values() if p.state == "open"},
        now)
    known = {str(i.id): i for i in items}
    wanted = {d for i in items for d in (i.depends_on or [])} - set(known)
    known |= await _load(session, wanted)
    human_blockers = await _human_blockers_for(session, items)

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
            waiting_on_a_human=human_blockers.get(str(item.id), ()),
            mine=mine, session_id=session_id,
            reconciled=reconciled.get((item.repo, item.ref_kind, item.ref_value)),
        )
        for item in items
    ]


async def _human_blockers_for(session: AsyncSession,
                              items: list[PlanItem]) -> dict[str, list[Blocker]]:
    """Open blockers keyed by the plan item they are about (#328, #555).

    A SECOND source feeding ``blocked_by``; the item-to-item edges are unchanged
    and this does not touch them. The two kinds are reported apart because the
    REMEDY is different — one waits on work, the other waits on a person — which
    is the argument `_next_caveat` already makes about the kinds it can see.

    **Two ways a blocker reaches an item, and the second one is #555.** An
    ``item`` blocker names the plan row directly. An ``issue`` or ``pr`` blocker
    names the *forge* — and an item that carries that ref is the same work, so it
    is attached here too.

    That second path is what makes an escalation partition the plan rather than
    merely report itself. Every producer the fleet has raises the forge kind:
    :func:`harness.loops.needs_human._subject_from` prefers ``pr`` then ``issue``
    and documents ``item`` as the kind "nothing emits today", because a loop
    reviewing a pull request knows the PR number and has never heard of a plan.
    So while this matched ``item`` alone, the rows the fleet actually produces
    attached to nothing, ``next`` kept handing the work out, and the queue #328
    built was invisible to the one reader that decides what happens next. #520
    measured the table at 0 rows; the rows that arrived afterwards had nowhere to
    land.

    **Exactly one open item can carry a given ref**, so the mapping cannot be
    ambiguous: ``ix_plan_items_open_ref`` is UNIQUE on
    ``(COALESCE(repo, ''), ref_kind, ref_value)`` where the item is open and the
    ref is present. The scope comparison here is that index's key, spelled the
    same way — an item and a blocker that disagree about the repo are about two
    different issues numbered 42, and `app.claimkey` already treats the repo as
    part of what a bare number means.

    A blocker naming an issue or PR that no open item points at still attaches to
    nothing, and that is unchanged and correct: it is a real question in the
    queue, and there is no plan row for it to hold up.
    """
    open_items = [i for i in items if i.state == "open"]
    if not open_items:
        return {}
    open_ids = [str(i.id) for i in open_items]
    # (repo, kind, value) -> item id, in `ix_plan_items_open_ref`'s spelling.
    by_ref: dict[tuple[str, str, str], str] = {
        (i.repo or "", i.ref_kind, i.ref_value): str(i.id)
        for i in open_items if i.ref_kind and i.ref_value
    }
    reaches = [(Blocker.subject_kind == "item") & Blocker.subject_value.in_(open_ids)]
    if by_ref:
        # COALESCE on the blocker side as well: `repo` is nullable on both tables
        # and NULL never equals NULL, so a fleet-scope blocker on a fleet-scope
        # item would drop out of an ordinary comparison.
        reaches.append(tuple_(func.coalesce(Blocker.repo, ""), Blocker.subject_kind,
                              Blocker.subject_value).in_(list(by_ref)))
    rows = (await session.execute(
        select(Blocker).where(or_(*reaches), Blocker.resolved_at.is_(None))
        .order_by(Blocker.raised_at))).scalars().all()
    out: dict[str, list[Blocker]] = {}
    for b in rows:
        item_id = (b.subject_value if b.subject_kind == "item"
                   else by_ref.get((b.repo or "", b.subject_kind, b.subject_value)))
        if item_id is not None:
            out.setdefault(item_id, []).append(b)
    return out


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

    **It renumbers the scope's open items rather than adding one to every rank
    past a number**, and that is the difference between keeping the promise and
    nearly keeping it. Ranks are not guaranteed distinct: a dropped item keeps the
    rank it had while a reorder renumbers everything still open around it, so a
    human putting it back can leave two open items sharing a rank — a state the
    list survives (the read breaks ties on ``created_at``, so it stays total) and
    that arithmetic on rank alone cannot place into. Anchored to the first of two
    items sharing rank 3, ``rank + 1`` puts the new row after BOTH of them, which
    is not what "immediately after that item" says. Reading the order and writing
    it back 1..n puts the row exactly where the caller asked, and repairs the
    duplicate on the way past without asserting anything — every existing pair
    keeps the relative order it was read in, which is the whole rule placement
    lives under.

    **OPEN items only, exactly as a reorder renumbers only open ones.** History
    keeps the rank it had — a done row is a record of finished work, not a
    position — so renumbering it would rewrite the record every time somebody
    placed an item above it.

    ``updated_at`` is deliberately NOT touched on the rows that move. Staleness is
    "has anybody paid this item any attention", and being renumbered is not
    attention: bumping it would let one placement make a fortnight-old plan read
    as fresh, which is precisely the plan that is believed and wrong.
    """
    items = await _scope_items(session, repo, exact=True, include_done=False)
    at = next((n for n, item in enumerate(items) if item.id == anchor.id), None)
    if at is None:  # pragma: no cover — `_resolve_anchor` just read it in this scope
        raise _place_refused("after", str(anchor.id), "no longer in this scope",
                             "read the plan again: it moved while this was in flight")
    # How many existing items end up ABOVE the new one.
    above = at if before else at + 1
    for n, item in enumerate(items):
        want = n + 1 if n < above else n + 2
        if item.rank != want:
            item.rank = want
    return above + 1


async def _top_rank(session: AsyncSession, repo: str | None) -> int:
    """The rank a picked-up item takes: above everything open in its scope.

    :func:`_place_rank` with no anchor and ``above = 0``, and it renumbers for the
    same reason — ranks are not guaranteed distinct, so arithmetic on ``min(rank)``
    cannot reliably produce a position above two rows that share one. Reading the
    order and writing it back from 2 puts the new row first and repairs any
    duplicate on the way past, while every existing pair keeps the relative order
    it was read in. Call under :func:`_lock_scope`, like its two siblings.

    ``updated_at`` is untouched on the rows that move, exactly as in
    :func:`_place_rank`: being renumbered is not attention paid to an item, and
    bumping it would let a busy afternoon of claims make a fortnight-old plan read
    as fresh.
    """
    items = await _scope_items(session, repo, exact=True, include_done=False)
    for n, item in enumerate(items):
        if item.rank != n + 2:
            item.rank = n + 2
    return 1


async def item_for_claim(session: AsyncSession, *, kind: str, key: str, holder: str,
                         note: str | None = None,
                         title: str | None = None) -> PlanItem | None:
    """The plan item a fresh claim implies, created at the top. None if the key names no work.

    **Picking work up is the one act that should put it on the board, and it was
    the only one that did not** (#427). The claim and the item were already two
    halves of one fact — :func:`claim_key` derives, for an issue-backed item, the
    very same ``(kind, key)`` a bare ``POST /claim`` takes — but the join only ran
    one way: ``GET /plan`` builds its key set *from the items it has*, so a claim
    with no item behind it was looked up by nobody and rendered nowhere.

    Called from ``POST /claim`` and deliberately NOT from :func:`acquire`, which
    :func:`claim_item` also goes through — an item claiming itself into existence
    is a loop with nothing at the end of it.

    Four things it will not do:

    * **Write an item for a key that is not a unit of work.** That judgement is
      :func:`~app.claimkey.work_ref`'s, and it declines merge claims, board
      objects, path keys and the open namespace.
    * **Fail because the item is already there.** The duplicate-ref index is the
      authority and its answer is success: the second agent to claim a planned
      issue gets the existing row back. The claim is the thing that prevents
      duplicated work; the item is a consequence of it.
    * **Invent a description.** The title is the claim's ``note`` — already
      specified as "one line on what you are doing with it", which is what a plan
      title is — or one a client that read the forge chose to pass. Absent both it
      is the ref, because the server cannot read GitHub (#327, and on purpose) and
      a made-up handle is worse than a bare one.
    * **Belong to a plan.** ``plan_id`` stays NULL: which of a repo's plans this
      work sits under is a judgement about the work, and nothing at claim time
      knows it. A human can move it.
    """
    ref = work_ref(kind, key)
    if ref is None:
        return None
    ref_kind, repo, number = ref

    existing = await _open_item_for_ref(session, repo, ref_kind, number)
    if existing is not None:
        return existing

    # Held to the commit for the same reason `add_item` holds it: `_top_rank` is a
    # read-then-write over every open rank in the scope, and two claims landing
    # together from one snapshot is a lost update with no unique index behind it.
    await _lock_scope(session, repo)
    item = PlanItem(
        repo=repo, title=_pickup_title(title, note, number),
        ref_kind=ref_kind, ref_value=number, plan_id=None,
        note=f"Picked up by {holder}. Added by the claim, not by a human — its "
             f"position says it is in flight, not that it outranks anything below "
             f"it on merit (#427).",
        depends_on=[], added_by=holder, rank=await _top_rank(session, repo),
        rank_source="picked-up", placed_for=None,
    )
    session.add(item)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if not is_unique_violation(e):
            raise
        # Lost the race to another claim or to a hand-written add. The row that
        # won is the answer — same reason `acquire` re-reads its winner rather
        # than reporting a generic failure.
        return await _open_item_for_ref(session, repo, ref_kind, number)
    await session.refresh(item)
    return item


def _pickup_title(title: str | None, note: str | None, number: str) -> str:
    """A handle for work nobody has written a title for, in descending order of truth.

    A client that read the forge knows the real one. Failing that the claim's own
    note is what the holder said they were doing, which is the same sentence a
    title wants. Failing both, the ref — short, true, and visibly a placeholder,
    which is the point: a reader who sees it knows to open the issue rather than
    believing a handle somebody's code made up.
    """
    for candidate in (title, note):
        text = _norm_text(candidate)
        if text:
            return text[:MAX_TITLE]
    return f"#{number}"


async def _open_item_for_ref(session: AsyncSession, repo: str | None,
                             ref_kind: str, ref_value: str) -> PlanItem | None:
    """The open item for a ref in a scope, if there is one — the duplicate index's own shape."""
    return await session.scalar(
        select(PlanItem).where(
            PlanItem.ref_kind == ref_kind, PlanItem.ref_value == ref_value,
            PlanItem.state == "open",
            PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo,
        ).order_by(PlanItem.rank).limit(1))


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
    #: not an agent's own decision to make (#478).
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


class ExemptIn(BaseModel):
    """Ask for, grant, or withdraw an exemption from the review queue (#335).

    The item is named either by ``item_id`` or by the PR it is about
    (``repo`` + ``pr``) — the second because an agent thinks in PR numbers and
    made-up spellings of "which item" is how an endpoint stops being called.
    """

    item_id: uuid.UUID | None = None
    repo: str | None = Field(default=None, max_length=200)
    #: ``"331"`` or ``"#331"`` — the same normalisation every other ref gets.
    pr: str | None = Field(default=None, max_length=64)
    #: Why. Required in both directions and for the same reason #279 gives about
    #: a bare flag: an exemption with nothing behind it is the confident
    #: assertion #67 warns about, and it costs somebody an interruption.
    reason: str = Field(min_length=1, max_length=MAX_EXEMPT_REASON)
    #: ``true`` asks for (agent) or grants (person) the exemption; ``false``
    #: withdraws a request or revokes a granted one.
    grant: bool = True
    session: str | None = Field(default=None, max_length=MAX_SESSION)

    @field_validator("reason")
    @classmethod
    def _a_flag_costs_a_reason(cls, v: str) -> str:
        """Blank is not a reason, and ``min_length=1`` lets ``"   "`` through.

        The same normalisation :func:`_norm_text` applies to every other stored
        free-text field, applied here because this one goes on to a person's
        interruption queue: #279 refuses a bare needs-human flag at the API and
        at the database CHECK, and an exemption asked for with nothing behind it
        is that flag under another name.
        """
        try:
            said = safe_reason(v)
        except MarkerInReason as e:
            raise ValueError(str(e)) from None
        if not said:
            raise ValueError("a reason cannot be blank: say why review can be "
                             "skipped, because somebody has to judge it")
        return said


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


class ScopeIn(BaseModel):
    """A scope being declared. ``name`` takes the sigil or goes without it.

    Going without it is safe **only here**, and only because of who is calling:
    this endpoint is behind :func:`app.auth.human`, so a person has already said
    which namespace they mean by arriving at it at all. Every path an agent can
    reach still refuses a bare name with ``REPO_SHAPE``, which is #148's rule and
    the thing #323 must not weaken.
    """

    name: str = Field(min_length=1, max_length=MAX_SCOPE)
    #: What this scope is, in the declaring person's words. A repo scope has a
    #: GitHub page to answer that; this has only what is typed here, so the field
    #: exists and is worth filling in.
    note: str | None = Field(default=None, max_length=MAX_NOTE)


# ------------------------------------------------- scopes, as declared things


def _scope_view(scope: PlanScope) -> dict:
    """One declared scope as it reads. ``name`` is canonical, ``label`` is spoken."""
    return {
        "scope": scope.name,
        "label": project_name(scope.name),
        "note": scope.note,
        "added_by": scope.added_by,
        "created": scope.created_at.isoformat(),
    }


class ReconcileFindingIn(BaseModel):
    """One ref a pass had something to say about.

    ``condition`` is not constrained to a known set on purpose. The list belongs
    to `qb-reconcile` and will grow there first, on a host that updates when its
    harness does — and a board that rejected a whole pass for carrying one word it
    had not been taught would fail closed on exactly the day somebody added a
    condition. An unknown one is stored and shown; :func:`_reconciled_caveat` says
    what it can about it and no more.
    """

    ref_kind: Literal["issue", "pr"]
    ref_value: str = Field(min_length=1, max_length=64)
    condition: str = Field(min_length=1, max_length=64)
    #: The pass's own sentence. Quoted back rather than re-derived, so the plan
    #: reports what was observed and not this board's paraphrase of it.
    said: str | None = Field(default=None, max_length=2000)


class ReconcileIn(BaseModel):
    """One scope's reconcile pass, entire.

    **Entire is the contract**: what arrives replaces what that scope had. A pass
    reports what it still finds, so a ref that has stopped being reported has
    stopped being true — resolution needs no separate call and cannot be forgotten
    in one. It also makes the write idempotent, which matters more than it sounds:
    two hosts run this timer and report the same pass minutes apart, and under
    append semantics the board would slowly fill with two of everything.
    """

    repo: str = Field(min_length=1, max_length=MAX_SCOPE)
    findings: list[ReconcileFindingIn] = Field(default_factory=list, max_length=500)


@router.post("/plan/reconcile")
async def report_reconcile(
    body: ReconcileIn,
    reporter: str = Depends(author),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record what a reconcile pass found about one scope's refs (#463).

    **The board cannot compute this.** It has no forge and #327 says it should not
    grow one, so whether an issue is closed is knowable only to a client holding
    `gh`. This is the door that observation comes through.

    **It changes no plan state, and that is the design rather than a limitation.**
    Nothing here marks an item done, drops it, or reorders anything: `next` still
    returns a flagged item rather than skipping it, and `state` stays whatever a
    person or an agent set. `qb-reconcile`'s own refusal to write is right about
    the conditions that are decisions — `dropped_candidate` in particular, where
    "the work was abandoned" is a judgement and inferring it would erase the
    distinction the plan's model exists to keep. What was missing was never the
    decision; it was that the observation and the plan were two facts on one board
    that never met. `plan_read` carries this now, and says it in `next.caveat`.

    Agent-authenticated, unlike the plan's ORDER, which needs a person or a
    delegated credential (#478). Ordering is
    the fleet's shared intent and an agent rewriting it makes the plan thrash;
    reporting what GitHub says about a ref is not intent, and the only agent that
    can report it is one somebody already trusted with a machine token.
    """
    now = datetime.now(UTC)
    seen: dict[tuple[str, str], ReconcileFindingIn] = {}
    for finding in body.findings:
        # LAST ONE WINS, and a pass that names a ref twice is not an error worth a
        # 400: `untracked_pr` and `note_contradicted` can both be true of one PR,
        # and refusing the report would lose the other four findings with it.
        seen[(finding.ref_kind, finding.ref_value)] = finding


    # ON CONFLICT, not read-then-write. Two hosts hold this timer and their passes
    # can land together, and the read above cannot see a row a concurrent request
    # is inserting — so the plain version loses the race on the unique constraint
    # and 500s, which the client reports as "not recorded" and the next tick
    # silently repairs. `first_seen` is deliberately absent from the update:
    # SURVIVING is what makes it mean "since", and it is the reason these are rows
    # and not a blob per pass — "a done candidate since Sunday" is the sentence
    # that turns a report into an argument, and no single pass can say it.
    for finding in seen.values():
        await session.execute(
            pg_insert(PlanReconcile)
            .values(repo=body.repo, ref_kind=finding.ref_kind,
                    ref_value=finding.ref_value, condition=finding.condition,
                    said=finding.said, first_seen=now, last_seen=now,
                    reported_by=reporter)
            .on_conflict_do_update(
                constraint="uq_plan_reconcile_ref",
                set_={"condition": finding.condition, "said": finding.said,
                      "last_seen": now, "reported_by": reporter,
                      # RESTARTED WHEN THE CONDITION CHANGES. `since` is quoted in
                      # the caveat as how long THIS has been true, so carrying it
                      # across a ref that was a stale claim on Monday and a done
                      # candidate on Wednesday would date the second from the
                      # first — a false sentence, and the one a reader would act
                      # on hardest. Unqualified here is the EXISTING row, and
                      # `excluded` would be the proposed one.
                      "first_seen": case(
                          (PlanReconcile.condition == finding.condition,
                           PlanReconcile.first_seen), else_=now)}))
    stored = len(seen)

    # ONE statement, evaluated against the table rather than against a list this
    # request read a moment ago. Two passes can land together — two hosts hold this
    # timer — and a read-then-delete lets each miss the other's inserts, leaving a
    # union of two reports that neither pass made. Deleting by "not in what I was
    # given" makes the loser's rows go with it: the outcome is one pass's set,
    # which is what replacing a scope's findings is supposed to mean.
    gone = await session.execute(
        delete(PlanReconcile).where(
            PlanReconcile.repo == body.repo,
            tuple_(PlanReconcile.ref_kind, PlanReconcile.ref_value).notin_(seen)
            if seen else text("true")))
    resolved = gone.rowcount or 0

    # THAT the pass ran, recorded beside what it found and never folded into it.
    # The delete above is why: a clean scope re-reports nothing, so every row goes
    # and `plan_reconcile` ends up empty — the same table state as a board nothing
    # has ever reconciled. Written unconditionally, including for the empty report,
    # because the empty report is exactly the case the findings cannot speak for.
    # `now()` is the server's, not `now` above: a host with a skewed clock would
    # otherwise be able to report a pass from the future and read as fresh until
    # the skew ran out.
    await session.execute(
        pg_insert(PlanReconcilePass)
        .values(repo=body.repo, last_pass_at=func.now(), reported_by=reporter)
        .on_conflict_do_update(
            constraint="uq_plan_reconcile_pass_repo",
            set_={"last_pass_at": func.now(), "reported_by": reporter}))

    await session.commit()
    return {"repo": body.repo, "stored": stored, "resolved": resolved,
            "reported_by": reporter, "at": now.isoformat()}


@router.get("/plan/reconcile")
async def read_reconcile_passes(
    caller: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """When a reconcile pass last covered each scope, and which machine ran it (#695).

    **The question is "is anyone reconciling", and it has no local answer.** The
    pass is a fleet singleton over a plan every host shares, so "is the timer
    enabled *here*" — which is what `qb-doctor` asked until #695 — is a question
    about the wrong noun. Three of five hosts in the fleet do not hold the units
    by design and were being called unwired for it. This is the fact that lets a
    host on which the pass is somebody else's job say so and still be honest about
    the case where it is nobody's.

    **Answering `null` is a real answer and not an error.** A scope no pass has
    covered has no row, and an empty `passes` means no scope has been reconciled
    at all — which is the original #255 failure, still worth reporting as one.
    Callers must not read the empty list as "fine": that is the collapse this
    endpoint exists to make impossible, and the reason a monitor is given the
    timestamp rather than a verdict.

    **No threshold here.** How old a pass may be before it is stale depends on the
    timer that writes it (fifteen minutes today) and on what the reader will do
    about it, and both live with the reader. The board holds when; whoever asks
    decides what that is worth.
    """
    rows = list(await session.scalars(
        select(PlanReconcilePass).order_by(PlanReconcilePass.last_pass_at.desc())))
    return {
        "passes": [
            {"repo": r.repo,
             "at": r.last_pass_at.isoformat(),
             "reported_by": r.reported_by}
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/plan/scopes")
async def list_scopes(
    caller: str = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The project scopes a person has declared — the scopes with no repo behind them.

    Repo scopes are deliberately absent: there is no list of them and there should
    not be one. A repo scope exists because a repository does, and enumerating them
    here would be a second register of something GitHub already holds — the "do not
    build a second store" rule this board keeps everywhere else.

    Read this to find out what a project scope is *called* before naming one. The
    board page reads it too, so a scope declared with nothing in it yet still
    appears in the picker; built from the items alone it would be invisible until
    somebody had already managed to add to it.
    """
    rows = list(await session.scalars(select(PlanScope).order_by(PlanScope.name)))
    return {"scopes": [_scope_view(r) for r in rows], "count": len(rows),
            "sigil": PROJECT_SIGIL}


@router.post("/plan/scope", status_code=201)
async def declare_scope(
    body: ScopeIn,
    editor: str = Depends(human),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Declare a scope with no repo behind it. **Human-only**, like reordering.

    Rule 3 says only a person orders the plan, because the plan is the fleet's
    shared intent and an agent rewriting it makes it thrash. *What the scopes are*
    is that decision one level up. An agent able to mint a scope could split the
    plan into lists nobody asked for and would do it silently — one mistyped
    ``project:65lowthr``, and the work is in a second list that no read reconciles
    against the first and no reader can see both halves of. That is #148's
    two-spellings defect wearing the plan's clothes, and the reason the declaration
    is explicit rather than inferred from "this does not look like a repo".

    **Idempotent, and that is not laxness.** Declaring a scope that exists returns
    the existing row with ``created: false``. The alternative is a 409 for a call
    whose whole content is "let this scope exist", which it already does — and a
    person on a phone tapping a button twice is not an error condition. What is
    refused is the thing that would actually be wrong: a name in the *repo*
    namespace, which is refused by shape before it reaches the database.
    """
    try:
        name = canonical_project(body.name)
    except BadRef as e:
        raise HTTPException(422, detail={
            "error": str(e), "name": body.name,
            "hint": "this endpoint declares a scope that is NOT a repo. A repo needs "
                    "no declaring — it is a scope already, spelled `owner/name`.",
        }) from None
    existing = await session.scalar(select(PlanScope).where(PlanScope.name == name))
    if existing is not None:
        return {**_scope_view(existing), "created": False}
    scope = PlanScope(name=name, note=_norm_text(body.note), added_by=editor)
    session.add(scope)
    try:
        await session.commit()
    except IntegrityError as e:
        # Somebody declared it between the read and the insert. The unique index is
        # what makes "one row per scope" true, and this is the losing side of it
        # doing the only correct thing — the same shape `_ensure_plan` takes.
        await session.rollback()
        if not is_unique_violation(e):
            raise
        raced = await session.scalar(select(PlanScope).where(PlanScope.name == name))
        if raced is None:  # pragma: no cover — the index just said otherwise
            raise
        return {**_scope_view(raced), "created": False}
    await session.refresh(scope)
    return {**_scope_view(scope), "created": True}


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
    repo = await _norm_scope(session, repo)
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
    # `waiting_on_a_human` joins the reasons `next` passes an item over, and it
    # has to: the whole failure #328 measured is an item parked on a decision
    # reading as ordinary open work and being handed to the next agent that asks.
    # A drain at `eager` (#474) would pick up the item whose own note says "RANK
    # IS WRONG AND A HUMAN MUST FIX IT" for exactly this reason.
    unclaimed = [v for v in open_views
                 if not v["claim"] and not v["blocked_by"] and not v["covered_by"]
                 and not v["waiting_on_a_human"]]
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
        # The declared project scopes, on the ONE call an agent makes cold (#323).
        # Not a second tool and not a thing to know to ask for: an agent learns
        # this API from the read it already makes, and a scope nobody can find the
        # name of is a scope nobody puts work into. Tens of rows at the outside —
        # a scope is a name for a body of work, not a row per piece of it.
        "scopes": [_scope_view(r) for r in await session.scalars(
            select(PlanScope).order_by(PlanScope.name))],
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
            # Two kinds, counted apart, because the remedy is different and
            # `plan_counts` consumers render them differently: one waits on work
            # finishing, the other on somebody answering. `blocked` keeps its old
            # meaning exactly — item-to-item edges — so a reader that predates
            # #328 is not silently told a new number.
            "blocked": sum(1 for v in open_views if v["blocked_by"]),
            "waiting_on_a_human": sum(1 for v in open_views
                                      if v["waiting_on_a_human"]),
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
    :func:`reorder`, which is unchanged in what it MEANS: an agent may apply an
    order a person asked for (#478) and may never decide one.

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
    repo = await _norm_scope(session, body.repo)
    _refuse_forge_ref(repo, body.ref_kind)
    _refuse_agent_exemption(body.ref_kind, body.note)
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
        existing = await _open_item_for_ref(session, repo, body.ref_kind, ref_value)
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
    out = {**view, "claimed": True, "renewed": renewed, "claim_id": str(claim.id),
           "forced": bool(blockers)}
    # The other pickup path (#568). `get-involved` comes through here rather than
    # through `POST /claim`, and an item claim writes the same row on the same key
    # — so the redirect has to hang off the claim both of them take, or the plan
    # route silently loses it. See `previous_lapse` for why a fresh take only.
    if not renewed:
        out.update(await lapse_hint(session, CLAIM_KIND, claim_key(item),
                                    exclude=claim.id, now=now))
    return out


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

    **And exactly one of them transitions the row (#723).** "Whoever observes it
    may write it down" was implemented as read-modify-write with no lock and no
    condition: every caller that found the row open stamped `done_by` over the
    last one and appended its receipt either way. That was tolerable while the
    observers were people and the agents they were driving. `qb-reconcile --apply`
    made it the ordinary case — an actor on a fifteen-minute timer on every
    machine in the fleet, each reading the same open row and writing the same
    completion — so an item finished by the fleet collected one receipt per host.

    The transition is now a conditional UPDATE, `open -> done`, decided by the
    database. `changed` says whether this call is the one that made it; a caller
    that lost gets `changed: false` beside the winner's `done_by`, and its
    receipt is not appended a second time. What did NOT change is who may call:
    the losing caller is answered 200 with the row's true state, not 409, because
    "somebody already recorded this" is the answer the caller wanted.
    """
    item = await _get(session, body.item_id)
    if item.state == "dropped":
        raise _dropped(item)
    # The COMPLETING agent's own words, never the note it is appending to: an
    # exemption a person already granted must not make its own PR impossible to
    # record as finished. A completed item exempts nothing anyway — the queue only
    # reads OPEN items — but a human may reopen one, and that is the seam.
    _refuse_agent_exemption(item.ref_kind, body.note)
    now = _utcnow()
    said = _norm_text(body.note)
    claim = await live_claim(session, CLAIM_KIND, claim_key(item), now)
    mine = claim is not None and _is_mine(claim, holder, body.session)
    left = None if mine or claim is None else claim_view(claim)
    if claim is not None and mine:
        claim.released_at = now
    # Conditional UPDATE, not read-then-write — `renew_claim`'s pattern, for the
    # reason its comment gives: the predicate is evaluated by the database at
    # write time, so the row cannot move between the look and the write. The
    # expected state the issue asks for is spelled as that predicate rather than
    # taken in the body, because every caller of `done` expects the same one and
    # a field that is always `"open"` is a constant the caller has to type.
    stamped: dict = {"state": "done", "done_at": now, "done_by": holder,
                     "updated_at": now}
    if said:
        stamped["note"] = _completion_note_sql(said)
    won = await session.execute(
        update(PlanItem)
        .where(PlanItem.id == item.id, PlanItem.state == "open")
        .values(**stamped)
        .returning(PlanItem.id))
    changed = won.scalar_one_or_none() is not None
    if not changed:
        # Nothing transitioned: the row was already finished, another caller won
        # the race, or a human dropped it inside the gap the check above cannot
        # cover — that check read a row this transaction never locked. Under READ
        # COMMITTED the UPDATE above has already waited out whoever was writing,
        # so this reads the settled row and not the one we opened with.
        state = await session.scalar(
            select(PlanItem.state).where(PlanItem.id == item.id))
        if state is None:
            raise HTTPException(404, "plan item not found")
        if state == "dropped":
            raise _dropped(item)
        if said:
            # **The human path stays open.** A person's `plan_done` following an
            # agent's still gets to say something, and it still lands: what is
            # refused is the same sentence twice. `qb-reconcile --apply` runs on a
            # fifteen-minute timer on every machine in the fleet and `apply_note`
            # renders byte-identical text on each of them — no host, no timestamp —
            # so the duplicate this drops is exactly the fleet's and nobody else's.
            #
            # In SQL rather than in Python for the same reason as the transition:
            # the value is derived from the note as it is AT THE WRITE, so two
            # callers appending different words cannot overwrite one another the
            # way the read-modify-write did.
            #
            # `state == "done"` for the same reason again, and it is not
            # belt-and-braces: a person may reopen a done item, and the SELECT
            # above is a read this transaction did not lock either. Without the
            # predicate, a reopen landing in that gap gets a completion receipt
            # written onto live work.
            await session.execute(
                update(PlanItem)
                .where(PlanItem.id == item.id, PlanItem.state == "done",
                       ~_note_already_says(said))
                .values(note=_completion_note_sql(said), updated_at=now))
    await session.commit()
    # The row as the writes left it. `expire_on_commit=False`, so `item` still
    # holds what the opening SELECT read — which, now that the write is a Core
    # UPDATE and not an attribute assignment, is a row that never went done.
    await session.refresh(item)
    view = (await _view_items(session, [item], now, mine=holder,
                              session_id=body.session))[0]
    # The claim itself, not a bool: `done` no longer renders a claim on the item
    # (it is history), so "somebody else was holding this when it was recorded
    # finished" would otherwise be a fact with nowhere left to read it.
    #
    # `changed` says whether THIS call is what finished the row. It is the whole
    # of #723's reporting half: a caller that lost can read the answer instead of
    # inferring it from timestamps, and `done_by` beside it names who won.
    #
    # It says nothing about what the row IS — `state` in the view does, refreshed
    # after the write, and the two answer different questions: `changed` is about
    # this call, `state` is about the row it left behind. A 200 carrying
    # `changed: false` always describes a done row, because the re-read above
    # answers its other two findings as errors rather than as a body: a missing
    # row is a 404 and a dropped one a 409. So a caller may read `done_by` beside
    # a `false` and know it names whoever transitioned it.
    return {**view, "claim_left": left, "changed": changed}


def _dropped(item: PlanItem) -> HTTPException:
    """A drop is a human decision that this should NOT happen.

    Letting an agent finish it anyway would route around the one rule the
    human-only endpoints exist to keep — quietly, and in the record. Raised from
    two places now: before the write, where it costs nothing, and after a
    transition that did not happen, where it is the only way to tell "somebody
    else recorded it done" from "a person dropped it while I was writing".
    """
    return HTTPException(409, detail={
        "error": "a human dropped this item", "item_id": str(item.id),
        "hint": "if the work happened anyway, ask for it to be reopened first"})


def _note_already_says(said: str) -> ColumnElement[bool]:
    """Is this exact receipt already a LINE of the item's note?

    **It assumes ``said`` is a single line, and refuses to answer when it is
    not.** A note is `\n`-joined and every element after the first carries
    :data:`_DONE_SEP`, so a receipt is present exactly when the padded text holds
    ``\n<said>\n`` (it was the first thing written) or ``\n— done: <said>\n``
    (it was appended). Padding both ends is what lets one `contains` answer for
    the head, the middle and the tail at once, instead of four anchored LIKEs —
    and it is also precisely what stops being sound once ``said`` may itself
    contain a newline. A note holding the receipts `foo` and `bar` reads
    ``\nfoo\n— done: bar\n``, which those two clauses match against a caller
    sending the two-line string ``"foo\n— done: bar"``: a different note,
    suppressed, which is the failure this path exists to avoid rather than an
    instance of the one it fixes.

    So a multi-line ``said`` is never recognised and always appended.
    ``DoneIn.note`` permits newlines, and nothing that would arrive with one is
    the duplicate being dropped here: the fleet's receipt is
    ``qb-reconcile``'s one-line :func:`apply_note` rendering, byte-identical
    across hosts because it carries no host and no timestamp. A multi-line note
    is somebody typing, and the answer to somebody typing is to write it down.

    The single-line predicate is narrow on purpose too — a plain substring test
    was the first cut, and it swallowed `landed in PR #143` because the row
    already read `landed in PR #143 after the schema change`. Both narrowings are
    the same rule: only the same sentence is the same sentence.
    """
    if "\n" in said:
        return false()
    padded = func.concat(literal("\n", Text), func.coalesce(PlanItem.note, ""),
                         literal("\n", Text), type_=Text)
    return or_(padded.contains(f"\n{said}\n", autoescape=True),
               padded.contains(f"{_DONE_SEP}{said}\n", autoescape=True))


def _completion_note_sql(said: str) -> ColumnElement[str]:
    """:func:`_completion_note`'s rule as an expression the DATABASE evaluates.

    The same append, against the note as it is at the moment of the write rather
    than as some earlier SELECT found it. That is what makes it safe to run from
    two hosts at once: `note = <text read a round trip ago> + receipt` silently
    dropped whichever concurrent append committed first, which is the second half
    of #723 and the half no reader would ever notice — the row is done either
    way, and only the sentence explaining it is missing.

    **Two implementations of one rule, deliberately, and pinned together by a
    test.** :func:`_completion_note` stays for ``finish_plan``, which appends to a
    *plan's* note through the ORM and is not written by anything on a timer; the
    shared constants (:data:`_DONE_SEP`, :data:`MAX_NOTE`) are the parts that can
    drift, and ``test_the_sql_append_and_the_python_one_say_the_same_thing``
    fails if they do. Rewriting ``finish_plan`` to match would be the same fix on
    an endpoint no ticket is about.

    ``literal(said, Text)`` and not a bare string: the ``THEN`` arm is a bind
    parameter with nothing around it to type it, and asyncpg asks the server to
    infer, which it cannot always do inside a ``CASE``.
    """
    merged = case((func.coalesce(PlanItem.note, "") == "", literal(said, Text)),
                  else_=PlanItem.note.concat(literal(_DONE_SEP + said, Text)))
    # `right(s, n)` is `merged[-MAX_NOTE:]`: the last n characters, or the whole
    # string when it is shorter.
    return func.right(merged, MAX_NOTE)


def _completion_note(existing: str | None, said: str | None) -> str | None:
    """Add the completion note to the item's note without destroying it.

    `note` is the reasoning for the item's position — "the sentence a human would
    otherwise repeat to each agent that asks". Editing it is deliberate and takes
    its own call: a person, or a delegated agent correcting reasoning that has gone
    stale (#478). What it is not is a side effect of finishing something. Replacing it with a completing agent's receipt
    ("landed in PR #143") deleted the intent and left the receipt in a field the
    agent was not allowed to write, unrecoverably.

    A PLAN's note, since #723 — an item's is appended by
    :func:`_completion_note_sql`, which states the rule as SQL so that two hosts
    completing the same item cannot lose one another's words. A plan is finished
    by an agent that decided to, never by a timer, so the race that forced the
    move does not reach here and the ORM write stays.
    """
    said = _norm_text(said)
    if not said:
        return existing
    merged = f"{existing}{_DONE_SEP}{said}" if existing else said
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
    editor: str = Depends(delegated),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Retitle, move, re-reason, or drop an item — a person; retitle and re-reason
    — a delegated agent.

    ``dropped`` is not ``done``: one says the work happened, the other says a
    person decided it should not. Reopening a dropped item is allowed here too,
    because a decision reversed is still a decision.

    A ``done`` item cannot be dropped. Dropping clears ``done_at``/``done_by``,
    so the drop control on a history row was one click from destroying the record
    that the issue ever closed — and the page offered it on every row.

    **A delegated agent may re-reason, and may not decide (#478).** #478's whole
    argument for a second credential is that it authorises a NAMED, narrow act
    rather than an identity, and the narrow act here is the one the changelog
    names: correcting reasoning an agent itself wrote and that has gone stale.
    Two of this endpoint's other powers are decisions and stay a person's:

    * **The review-exemption marker.** :func:`_refuse_agent_exemption`'s docstring
      says *"Only two paths can put it on an open item now, and both take
      app.auth.human: POST /plan/item/update and exempt_item's grant half"* — so
      widening this endpoint's gate without this guard reopened #335 through one
      of the two doors that #335's own fix depends on. Measured, not reasoned
      about: before this guard, a delegated agent writing a `review: exempt` note
      here got `exempt: True` on its own PR, which is precisely the authority
      ``exempt_item`` refuses it by downgrading a grant to a request.
    * **Dropping, and moving between plans.** *"a person decided it should not"*
      is the endpoint's own description of the first, and an agent deciding that
      about work it may be the one avoiding is the same self-approval shape one
      field over; it also reaches ``live_claim`` and clears somebody's hold.
      ``plan`` is the same kind of act — detaching an item from a plan somebody is
      holding changes what is grouped with what, and ``""`` detaches entirely.

    Both refuse the ACT and not the caller, so nothing an agent legitimately does
    here changes: a delegated note update is still one call.
    """
    _refuse_phase(body.phase)
    item = await _get(session, body.item_id)
    if not is_human(editor):
        # Ordered before every other check so a refusal is about the authority
        # and not about the item's state — an agent told "that item is done"
        # would reasonably conclude the write was otherwise allowed.
        # `state` and `plan` both DECIDE something; `title` and `note` describe.
        # `plan` joined this guard after a panel round escalated the gap: the
        # docstring above claimed title and note were the whole surface while
        # `plan` was applied for a delegated caller with no check at all, so an
        # agent could move an item between plans — or detach it from one a person
        # is holding, which reaches `covered_by` and is nearer to dropping than to
        # re-reasoning. Narrowed rather than documented, because widening later is
        # one line and discovering the reverse is not.
        refused = [f for f, v in (("state", body.state), ("plan", body.plan))
                   if v is not None]
        if refused:
            raise HTTPException(403, detail={
                "error": f"{' and '.join(refused)} on a plan item is a person's decision",
                "hint": "a delegated credential may retitle and re-reason an item "
                        "(`title`, `note`); it may not decide whether the work "
                        "should happen or which plan it belongs to. See #478.",
                "refused": refused,
                "item_id": str(item.id)})
        _refuse_agent_exemption(item.ref_kind, body.note)
        # And the other direction, which the guard above cannot see: `note` is a
        # WHOLE-FIELD replacement, so an agent writing an innocuous note over one
        # that carries the marker REVOKES a person's exemption — the PR silently
        # rejoins the review queue. Round 1 closed "an agent may not set it" and
        # left "an agent may not clear it" wide open; same field, same endpoint,
        # opposite direction. Measured: exempt True -> agent writes a note -> False.
        if body.note is not None and exempting(item.note):
            raise HTTPException(403, detail={
                "error": "that item carries a review exemption a person granted",
                "hint": "replacing the note would revoke it. Ask a person to change "
                        "or withdraw the exemption first (POST /plan/item/exempt "
                        "with grant: false), then re-reason the item.",
                "item_id": str(item.id)})
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


async def _exempt_target(session: AsyncSession, body: ExemptIn) -> PlanItem:
    """The open plan item this request is about, or a 4xx saying why there is none.

    An exemption is a marker on a plan item, so there has to be one. A PR with no
    item is not exempt and cannot be — *"silence is not exemption"* is #273's own
    rule — and the refusal says how to make one rather than inventing it here: an
    item created as a side effect of asking to skip its review is a row nobody
    ranked, in a plan nobody chose to put it in.
    """
    if body.item_id is not None:
        # Locked, not just read. Everything below is read-modify-write over one
        # free-text column: two proposals landing together would each see no
        # request and each write one, and — worse — a proposal that read the note
        # before a human granted would strip the grant back off on the way past.
        # An agent must not be able to undo a person's decision by racing it.
        item = await session.get(PlanItem, body.item_id, with_for_update=True)
        if item is None:
            raise HTTPException(404, "plan item not found")
    else:
        pr = _norm_ref(body.pr)
        if not pr:
            raise HTTPException(422, detail={
                "error": "name the item: `item_id`, or `repo` and `pr`",
                "hint": "an exemption is a marker on a plan item, so there has to "
                        "be one — add it with POST /plan/item first"})
        repo = await _norm_scope(session, body.repo)
        item = await session.scalar(
            select(PlanItem).where(
                PlanItem.repo.is_(None) if repo is None else PlanItem.repo == repo,
                PlanItem.state == "open",
                PlanItem.ref_kind == EXEMPTABLE_REF_KIND,
                PlanItem.ref_value == pr,
            ).with_for_update())
        if item is None:
            raise HTTPException(404, detail={
                "error": f"no open plan item for pr {pr} in {repo or 'the fleet scope'}",
                "hint": "silence is not exemption (#273): a PR with no plan item is "
                        "in the queue. Add the item with POST /plan/item, then ask."})
    if item.ref_kind != EXEMPTABLE_REF_KIND:
        raise HTTPException(422, detail={
            "error": f"item {item.id} names {item.ref_kind or 'nothing'}, not a pr",
            "item_id": str(item.id),
            "hint": "the review queue reads the marker off the plan item for a PR, "
                    "so an exemption on anything else would exempt nothing"})
    if item.state != "open":
        raise HTTPException(409, detail={
            "error": f"that item is {item.state}: only an open item exempts a PR",
            "item_id": str(item.id),
            "hint": "the queue reads open items only, so a marker here would be a "
                    "record of a decision and not the decision"})
    return item


def _exempt_note(item: PlanItem, line: str | None) -> str:
    """The item's note with every exemption line replaced by ``line`` (or none).

    Bounded rather than truncated. ``_completion_note`` trims from the left,
    which is right for a receipt appended to reasoning nobody will read again; it
    is wrong here, because the left of this note is the human's argument for the
    item's position and an exemption is not worth silently eating it.
    """
    rest, _ = strip_exemption_lines(item.note)
    merged = "\n".join(x for x in (rest, line) if x)
    if len(merged) > MAX_NOTE:
        raise HTTPException(409, detail={
            "error": "that item's note has no room for the exemption line",
            "item_id": str(item.id),
            "hint": f"a note is at most {MAX_NOTE} characters and this one is "
                    f"{len(item.note or '')}; shorten it, or shorten the reason"})
    return merged


def _exempt_view(item: PlanItem) -> dict:
    """What the note says about this item's exemption, in structured form."""
    granted = granted_exemption(item.note)
    pending = requested_exemption(item.note)
    return {
        "exempted": exempting(item.note),
        "granted": None if granted is None else {
            "by": granted.by, "reason": granted.reason},
        "requested": None if pending is None else {
            "by": pending.by, "reason": pending.reason},
    }


def _exemption_ask(item: PlanItem, who: str, reason: str, session_id: str | None) -> Post:
    """The board post that carries an exemption request to a person — #274's door.

    #274 measured the cost of four escalation paths and no destination: over
    thirty days and sixty-five rounds, ``deferred: 0`` and not one ``stuck`` post
    from any part of this repo's review machinery. Its answer was that every
    escalation leaves by one door, and this is that door — ``type='stuck'``,
    addressed to a person, carrying #279's class and reason — written from the
    board rather than from :func:`harness.loops.needs_human.announce` only
    because the refusal happens here and an announcement a caller has to remember
    to make separately is one that eventually is not made.

    Addressed to :data:`app.identity.HUMAN`, the bare namespace, which reaches
    every person on the board rather than one named account. There is no
    configured addressee to get wrong and nothing to leave unwired: whoever is
    reading the board on a phone is who this is for.
    """
    pr, repo = item.ref_value, item.repo
    where = f"[{repo}] " if repo else ""
    lines = [
        f"class:  {EXEMPT_DECISION_CLASS}",
        f"label:  {label_for(EXEMPT_DECISION_CLASS)}",
        f"reason: {reason}",
    ]
    if repo:
        lines.append(f"repo:   {repo}")
    lines += [
        f"item:   {item.id} ({item.title})",
        "",
        f"{who} has asked to take PR #{pr} out of the review queue. **It is not "
        "out.** An exemption is a human write (#335) — the same footing as "
        "reordering the plan — so the PR stays in the queue and stays drainable "
        "until a person grants this.",
        "",
        "Grant it on the plan page (/plan/view — the ⊘ on that row), or:",
        f'  POST /plan/item/exempt {{"item_id": "{item.id}", "reason": "…"}}',
        "Decline it with the same call and \"grant\": false.",
    ]
    return Post(
        author=who,
        session=session_id,
        type="stuck",
        summary=(f"{where}needs a human ({EXEMPT_DECISION_CLASS}): exempt PR "
                 f"#{pr} from review? {reason}").strip()[:900],
        detail="\n".join(lines),
        recipient=HUMAN,
        refs=[r for r in (
            {"kind": "pr", "value": str(pr), **({"repo": repo} if repo else {})},
            {"kind": "repo", "value": repo} if repo else None,
        ) if r is not None],
    )


async def _already_announced(session: AsyncSession, item: PlanItem,
                             now: datetime) -> bool:
    """Has this PR's exemption question already been put to a person recently?

    Matched on the post's own ``refs`` rather than on its prose, because the ref
    is the structured half and a summary is not a key. Counted over
    :data:`EXEMPT_ANNOUNCE_WINDOW` rather than for ever: a question a person has
    left unanswered for a day is one worth asking again.
    """
    ref: dict = {"kind": "pr", "value": str(item.ref_value)}
    if item.repo:
        ref["repo"] = item.repo
    return bool(await session.scalar(
        select(func.count()).select_from(Post).where(
            Post.type == "stuck",
            Post.recipient == HUMAN,
            Post.ts >= now - EXEMPT_ANNOUNCE_WINDOW,
            Post.refs.contains([ref]),
        )))


@router.post("/plan/item/exempt")
async def exempt_item(
    body: ExemptIn,
    who: str = Depends(author),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ask for an exemption from review, or — if you are a person — grant one.

    **The refusal and the request are one endpoint on purpose.** #335's whole
    argument is that an agent must not exempt its own PR, and the repo has twice
    learned the other half: a control with nowhere for the refused request to go
    is a control agents route around (#274 counted the result — four escalation
    doors, none of them the board, and thirty days of ``deferred: 0``). So the
    same call an agent makes to ask is the call a person makes to answer, and the
    only thing the caller's credential decides is which of the two it was.

    * **An agent** proposes. The request is written on the item as
      ``review: exempt-requested by <agent> — <reason>``, which the queue reports
      and no reader can mistake for the exemption itself, and it is announced on
      the board as a ``stuck`` post addressed to :data:`app.identity.HUMAN`.
      Nothing about the PR's queue state changes: it stays in the queue, it stays
      drainable, and it stays reviewable. **That is not an oversight.** Letting a
      pending request hold a PR out of review would hand the worker, by a longer
      route, exactly the authority this endpoint exists to withhold — an agent
      could suspend its own review indefinitely by asking.
    * **A person** disposes — ``grant: true`` writes the marker, ``grant: false``
      withdraws a request or revokes a granted exemption. A person is proved
      through :func:`app.auth.author` by the edge's ``X-Edge-Auth`` secret, which
      is the same proof :func:`app.auth.human` requires of the reorder that is
      known to work in production — not a second boundary invented here.

      The two differ on exactly one axis, and this endpoint is the stricter of
      them: ``human()`` lets ``BROWSER_DEV_HUMAN`` outrank a bearer token, and
      ``author()`` does not. That ordering is deliberate and it is the right one
      here — on a local board with that flag on, letting it win would make every
      agent's ``exempt`` call a grant, which is this endpoint's own hole reopened
      for the convenience of a dev bypass. ``BROWSER_DEV_HUMAN`` is off by
      default and DEPLOY.md's checklist requires it unset in production.

    Both halves are idempotent: asking twice is one request, granting twice is
    one exemption, and neither reposts to the board.

    **If the human path is not deployed, nothing is lost and nothing is wrongly
    granted.** With no ``HUMAN_EDGE_SECRET`` nobody is a person, so every call
    here is a proposal — requests accumulate, attributed and visible, and take
    effect when somebody can answer them. An exemption that waits is the safe
    direction; an exemption the subject grants itself is not.
    """
    item = await _exempt_target(session, body)
    reason = body.reason          # already one line, and not blank — see ExemptIn
    person = is_human(who)
    before = _exempt_view(item)

    async def _unchanged(why: str) -> dict:
        """Idempotent: the state is already what was asked for, so nothing moves.

        Not an error, and not a second board post. An agent that retries — or two
        agents on one PR — must cost the person reading the board one message,
        not one per attempt.

        **The same shape as the acting path**, ``item`` included. A response that
        loses a key on the branch a retry takes is a response a retry cannot read,
        and the retry is exactly the case this branch exists for.
        """
        return {"item_id": str(item.id), "pr": item.ref_value, "repo": item.repo,
                "by": who, "acted": False, "proposed": False, "announced": False,
                "post": None, "why": why, **before,
                "item": (await _view_items(session, [item], _utcnow(), mine=who))[0]}

    if person:
        if body.grant:
            line = grant_line(who, reason)
        elif not (before["exempted"] or before["requested"]):
            raise HTTPException(409, detail={
                "error": "there is no exemption or request on that item to withdraw",
                "item_id": str(item.id)})
        else:
            line = None
    elif body.grant:
        if before["exempted"]:
            return await _unchanged("a person has already exempted this PR")
        if before["requested"]:
            return await _unchanged(
                "an exemption has already been asked for on this item")
        line = request_line(who, reason)
    else:
        if before["exempted"]:
            raise HTTPException(403, detail={
                "error": "an agent may not revoke an exemption a person granted",
                "item_id": str(item.id),
                "hint": "if the PR should be reviewed anyway, review it — nothing "
                        "here stops that. Revoking is a person's call, on the same "
                        "footing as granting."})
        if not before["requested"]:
            raise HTTPException(409, detail={
                "error": "there is no request on that item to withdraw",
                "item_id": str(item.id)})
        if before["requested"]["by"] != who:
            # Yours to withdraw, or a person's to decline — not another agent's to
            # clear away. Withdraw-and-re-ask is also the one loop that gets past
            # the "already asked" branch, so leaving it open to anybody would make
            # a person's notification queue writable by every agent on the board.
            raise HTTPException(403, detail={
                "error": f"that request is {before['requested']['by'] or 'somebody'}'s, "
                         "not yours to withdraw",
                "item_id": str(item.id),
                "hint": "a person can decline it here; an agent can only take back "
                        "the request it made itself"})
        line = None

    now = _utcnow()
    announcing = not person and body.grant
    quiet = announcing and await _already_announced(session, item, now)
    item.note = _exempt_note(item, line) or None
    item.updated_at = now
    posted = (_exemption_ask(item, who, reason, clean_session(body.session))
              if announcing and not quiet else None)
    if posted is not None:
        session.add(posted)
    await session.commit()
    await session.refresh(item)
    if posted is not None:
        await session.refresh(posted)
    after = _exempt_view(item)
    return {
        "item_id": str(item.id),
        "pr": item.ref_value,
        "repo": item.repo,
        "by": who,
        "acted": True,
        "proposed": announcing,
        # Recorded is not the same as announced, and a caller that cannot tell
        # them apart will re-ask to make sure. `false` here means the request is
        # on the item and a person has already been told about this PR inside the
        # last hour — not that anything failed.
        "announced": announcing and not quiet,
        "post": None if posted is None else posted.id,
        **after,
        "item": (await _view_items(session, [item], _utcnow(), mine=who))[0],
    }


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
    editor: str = Depends(delegated),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set the order. A person's decision — which a delegated agent may APPLY.

    Items in scope that the caller did not list keep their relative order and
    follow the listed ones, and are named in ``appended``. A stale page must not
    be able to lose an item it never knew about — but it must not silently
    reshuffle it either, so the omission is reported rather than assumed.

    **Only open items have an order.** A dropped item used to be re-ranked by
    every reorder and named in ``appended`` while being absent from the ``items``
    the same response returned — so the page, the request and the reply each
    believed something different about a row nobody could see.
    """
    repo = await _norm_scope(session, body.repo)
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
    # WHO asked decides what the rank claims about itself (#478). A person's
    # sequence is `ordered`; a delegated agent's is `derived` — a rule and an
    # instruction produced it together, which is weaker evidence than a person
    # typing it and much stronger than an append. Writing `ordered` for both
    # would make an agent-applied order indistinguishable from a human's in the
    # one field a client can read, which is #183's substitution exactly.
    chosen = "ordered" if is_human(editor) else "derived"
    now = _utcnow()
    for rank, item in enumerate([*ordered, *rest], start=1):
        # `ordered` is the human's sequence and `rest` is what the page did not
        # know about, so only the listed items become `ordered`: an item that
        # arrived after the page loaded was carried along, not decided on, and
        # marking it would make `GET /plan` claim a human had chosen a position
        # they never saw (#183).
        source = chosen if item.id in listed else item.rank_source
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

    Repository names meet in one spelling on both sides. GitHub repos are
    case-insensitive, so a run recorded as ``PrisonBlues/quarterback`` would
    otherwise leave its PR looking like one the board had never seen — the
    silent-absence failure #101 is filed about wearing a different hat. This used
    to be a ``func.lower()`` over ``review_runs.repo``, which was stored as the
    panel sent it; since #326 the *write* folds, migration ``0033``'s CHECK
    constraint holds it there, and the column can be compared directly — which is
    also what lets ``ix_review_runs_repo_pr`` answer this query.

    The plan's own side keeps its ``.lower()``. ``_norm_scope`` canonicalises
    every scope written since #148, but rows predating it are still on the board
    (``app.api.claims`` re-folds them for the same reason), so folding here costs
    nothing and reaches them.

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

    # TWO newest runs per (repo, pr), because the evidence below answers two
    # different questions and #94 pulled them apart (#94).
    #
    # `state_runs` — the newest run of any kind, including a title-skipped merge.
    # It supplies the READINGS: `pr_state`, `draft`, `ci`. Those are observations
    # about the pull request, and the skip path fetches the same `gh pr view`
    # metadata as any other exit, so its reading is as good and newer. A PR
    # merged since its last review says MERGED here for the first time.
    #
    # `review_runs_` — the newest run that actually reviewed. It supplies the
    # FINDINGS and the provenance beside them. A skipped merge carries no
    # findings at all, so letting one answer would report every item on that PR
    # as clear of outstanding work and move it up the plan — the ordering
    # equivalent of a false all-clear.
    #
    # The split is along the seam this module already draws: `_PROVENANCE_FIELDS`
    # is deliberately kept apart from the readings so that neither is stored
    # twice and free to disagree. Provenance stays whole and comes from ONE run,
    # so `run_id`, `as_of`, `round`, `head_sha` and the two counts always
    # describe the same round; a caller reading `as_of` is told the age of the
    # REVIEW, which is the number `_stale` ages.
    #
    # `IS NOT FALSE` on the second, so every run recorded before the column keeps
    # answering exactly as it does today.
    def _newest(*extra):
        return (
            select(ReviewRun)
            .where(tuple_(ReviewRun.repo, ReviewRun.pr).in_(list(wanted)), *extra)
            .distinct(ReviewRun.repo, ReviewRun.pr)
            .order_by(ReviewRun.repo, ReviewRun.pr,
                      ReviewRun.ts.desc(), ReviewRun.id.desc())
        )

    state_runs = {(r.repo.lower(), r.pr): r for r in await session.scalars(_newest())}
    runs = list(await session.scalars(_newest(ReviewRun.reviewed.isnot(False))))
    # Confirmed findings on those runs, and which of them somebody has recorded an
    # outcome for. All five outcomes count as answered, `deferred` included: it
    # says the work was moved to an issue, which is a decision, and treating it as
    # outstanding would keep an item at the head of the plan for a finding
    # somebody has already dealt with. `narrowed` (#615) needs no argument here at
    # all — the code changed and the finding as raised is answered — and it is
    # covered without a code change because this reads the PRESENCE of a row and
    # not its value. NO outcome row is what counts as open — nobody has said,
    # which is neither fixed nor refuted (v2.37).
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
            .where(tuple_(ReviewFindingOutcome.repo,
                          ReviewFindingOutcome.pr).in_(list(wanted)))
        ):
            answered.add((repo.lower(), pr, key))

    by_pair = {(r.repo.lower(), r.pr): r for r in runs}
    evidence: dict[tuple[str, int], dict] = {}
    # Keyed off the state runs, which are a superset: a PR whose only run is a
    # skipped merge has readings and no review, and reporting it with a null
    # `outstanding_findings` puts it in `_unknown` — named as unread rather than
    # silently absent, which is what it was before this endpoint could see it.
    for pair, state in state_runs.items():
        run = by_pair.get(pair)
        keys = confirmed.get(run.id, set()) if run is not None else set()
        evidence[pair] = {
            # Provenance: the REVIEW's, whole and from one run, or absent.
            **({} if run is None else {
                "run_id": run.id,
                "as_of": run.ts.isoformat(),
                "round": run.round,
                "head_sha": run.head_sha,
            }),
            # Readings: the newest observation of the PR, whoever made it.
            "pr_state": state.pr_state,
            "draft": state.is_draft,
            "ci": state.ci_status,
            # Only CONFIRMED findings count as work, matching every other number
            # this board publishes about a round — and the count that was left out
            # rides along beside it, because a rule that quietly ignores a
            # category is a rule nobody can argue with. A finding no judge ruled
            # on is not evidence of anything yet (v2.37), and a round recorded
            # with `judged: false` is all unjudged.
            **({} if run is None else {
                "unjudged": run.n_unjudged,
                "confirmed": len(keys),
                "outstanding_findings": sum(
                    1 for k in keys if (*pair, k) not in answered),
            }),
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
        # No evidence at all, or evidence with no REVIEW in it (#94). The second
        # is a PR the board has only ever seen skipped: it has a reading — the
        # skip path fetches the same metadata as any other exit, so `pr_state`
        # and `ci` are real and feed the rules — and no round has ever looked at
        # the code. `as_of` is the age of a review, so there is none to age, and
        # the honest bucket is the same one: nobody has reviewed this.
        if ev is None or "as_of" not in ev:
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
            "reason": "the board has never recorded a panel that REVIEWED this PR — it "
                      "has either never seen the PR at all, or seen it only on a round "
                      "that reviewed nothing (a title-skipped merge). It knows only the "
                      "PRs it has panelled, so this is not evidence the PR is fine",
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
            # False since #478: `POST /plan/reorder` takes a person OR an agent
            # holding its machine's delegated credential. Clients BRANCH on this
            # field — it is not prose — so leaving it True told every reader that
            # applying the suggestion was impossible for them.
            "human_only": False,
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
    repo = await _norm_scope(session, repo)
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
    repo = await _norm_scope(session, body.repo)
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
            "human_only": False,
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
    scope = await _norm_scope(session, repo)
    stmt = select(OrderProposal)
    if repo is not None:
        stmt = stmt.where(OrderProposal.repo == scope)
    elif exact:
        stmt = stmt.where(OrderProposal.repo.is_(None))
    rows = list(await session.scalars(stmt.order_by(OrderProposal.id.desc()).limit(limit)))
    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())) or 0
    return {
        "repo": scope,
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
    repo = await _norm_scope(session, repo)
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
    repo = await _norm_scope(session, body.repo)
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
        _refuse_forge_ref(repo, item.ref_kind, position=n)
        _refuse_agent_exemption(item.ref_kind, item.note, position=n)
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
                # unchosen position, and `first_unchosen` names the seam.
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
