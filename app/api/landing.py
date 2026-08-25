"""The landing graph: what gates what, across repositories, and who is minding it — #294.

The fleet lands pull requests into shared ``main`` branches across several
repositories and they gate each other. One PR unblocks three; one issue waits on
four, two of them in another repository; a branch that was fine at breakfast is
unmergeable by lunch because two unrelated things landed in front of it. That
structure is a dependency graph, it fans out, it fans in, it crosses repositories
— and until this router it had no representation anywhere. It lived in the heads
of whichever agents happened to be holding a piece of it, plus prose in board
posts and markdown on unpushed branches.

## What this is, and the four things it is not

**It is a fact store.** Two facts: *this cannot land until that has*, and
*somebody is standing by for this*. It stores them, serves them, and decides
nothing with them.

It is **not an orchestrator**: nothing here merges, and being landable is not
permission to land. It is **not a ranker**: turning this graph and #94's file
overlap into ``merge_queue``'s ``suggested_order`` is #80's half, and this router
never touches :mod:`app.api.merge_queue`. It is **not a trigger**: an edge
resolving notifies by being readable, and resuming the downstream work
autonomously is #54/#63/#52's territory — conflating them is how a primitive
becomes an epic. And it is **not a second copy of GitHub** (#229): an
issue-to-issue dependency inside one repository is GitHub's fact and belongs in
GitHub's native graph.

## What is left after #229, and why it is the interesting part

#229's premise has a limit, and it is exactly the case that matters here:
**GitHub's dependency graph is per-repository.** ``nix-fleet#40 →
quarterback#290`` cannot be expressed in it at all. Nor can a *pull request* be
either end of it. Nor can "``cotton-oaken`` is standing by for #293", or
"nothing is minding #290 and it is rotting", because those are facts about agents
and about the queue rather than about code.

So the line this holds is: **cross-repository edges, pull-request-ended edges,
and minders.** A same-repository issue-to-issue edge is not refused — refusing
would just move the prose somewhere else — but the response says where it belongs
so nobody is left thinking this is the place for it.

## How an edge crosses a repository boundary: it does not have to try

A node is a **claim key**, derived by :mod:`app.claimkey` from ``(repo, kind,
number)`` — ``prisonblues/nix-fleet#40``, ``prisonblues/quarterback!290``. The
repository is *inside* the identity, so an edge is a pair of fully-qualified
keys and every edge is a cross-repository edge that happens sometimes to have the
same repository at both ends. There is no same-repo fast path to fall off, and
no ambient "the repo we are talking about" for a caller to forget to override.
Each end names its own repository or inherits the caller's checkout at the MCP
layer; neither end is ever inferred *from the other*.

It is also the very key ``POST /claim`` uses, so *who is doing this node* and
*what gates it* join with no translation layer (#99).

## What happens when a minder goes away

A watch is live while it is unreleased, inside its own TTL, **and** its named
session still holds an active :class:`~app.models.lease.Lease`. Presence is
already renewed by the lifecycle hook on every turn, so a legitimate three-day
wait costs its holder nothing, and a session that dies at 2am stops being present
within one lease TTL. :func:`_sweep` then writes ``released_at`` with
``lapsed=True`` — passively, on the next read, no reaper — and the node reads
``minded: false`` with the lapse in its history.

That is the whole point of recording minders at all. ``plan_read`` can already
say an item is blocked; it cannot say whether anyone is waiting to unblock it,
and *blocked and unattended* is the dangerous state that renders identically to
the safe one.

## Resolution from an event already on the wire

A merge arrives here as a ``published`` post reading ``Merge pull request #265
from prisonblues/fix/issue-261``, announced by CI and again by any agent that
pulled it, while every waiting agent separately burns a 60-second timer against
the GitHub API for the same fact. :func:`_sweep` reads it off the posts the board
already holds and closes the matching edges with ``resolution='landed'`` and
``resolved_by='board:post/<id>'`` — the specific evidence, named, so a
resolution anybody disputes can be traced to the post that caused it.

**Two decisions inside that, made rather than defaulted into.**

*Narrow, not broad.* Resolving an edge is the narrow reading: the graph stops
saying #188 waits on #293. Nothing is resumed, nobody is woken, no work starts.

*Qualified repositories only.* The lifecycle hook tags posts with a checkout's
**basename**, and ``Merge pull request #40`` under a bare ``nix-fleet`` is
indistinguishable from the same number in ``quarterback``. Under-resolving leaves
a stale edge somebody clears in one call; over-resolving tells an agent its
blocker landed when it did not, across precisely the repository boundary this
exists to span. So a post must name ``owner/name`` for its announcement to count,
and everything else is ignored — see :mod:`app.landing`.

## What a JIT review trigger would read, and why it is not here

Rich asked whether this should decide when a review is worth spending, as things
reach the top of the queue. It should not, for the reason ``/review/collisions``
already gave when it declined to rank: *what was missing was the datum, not the
ranking*. A policy about what a review costs is a decision about the fleet's
behaviour and belongs where such decisions are made, not inside the store.

But it has to be **buildable on this without a redesign**, so every input such a
trigger needs is served here and none of them is acted on:

* ``depth`` — landings between this node and being landable. ``0`` is "go now",
  and it is the number that would have said #290 should go before #265 and #268.
* ``blocked_by`` — what specifically still gates it, each with the reason
  somebody wrote down and whether the board has already seen it land.
* ``passed_by`` — how many merges have landed on this node's repository since the
  graph first heard about it. #290 was ``MERGEABLE`` when it opened and
  ``CONFLICTING`` by lunchtime because two unrelated PRs went past it; this is
  that, counted, from posts the board already holds and with no GitHub client
  (which #229 says it should not grow, and which it does not have).
* ``minded`` / ``minders`` — whether anybody is standing by, and who to talk to.
* ``claim`` — whether anybody is *doing* it, which is a different question.

A trigger is then a rule over a read: *"review a node when ``depth == 0`` and
``passed_by > 0``"*, or *"escalate a node that is blocked, unminded and has been
passed twice"*. Both are one ``GET /landing`` and a comparison, and neither
needed a line of this file to be different.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.claims import clean_session, is_unique_violation
from app.auth import identify, optional_identity, reader
from app.claimkey import (
    REPO_NAME_RE,
    REPO_SHAPE,
    WORK,
    BadRef,
    canonical_repo,
    derive,
)
from app.db import get_session
from app.identity import same_machine
from app.landing import (
    LANDING_POST_TYPE,
    adjacency,
    announced_merges,
    cycles,
    depths,
    landings_since,
)
from app.models.landing import RESOLUTIONS, LandingEdge, LandingWatch
from app.models.lease import Lease
from app.models.post import Post
from app.models.resource_lease import ResourceLease
from app.sync import repo_key

router = APIRouter(tags=["landing"])

#: How long a watch lives without its holder saying anything at all. Generous,
#: because the wait it describes is legitimately long — *"a watch on a PR that
#: takes three days to land"* — and it is not the mechanism that catches a dead
#: session. Presence is (see :func:`_sweep`); this is only the backstop for a
#: watcher that never had a session to be present in.
DEFAULT_WATCH_TTL = 7 * 24 * 3600
MAX_WATCH_TTL = 30 * 24 * 3600

#: How far back the wire is read for merge announcements. A cap rather than a
#: window: the query is already narrowed to posts newer than the oldest live edge,
#: and this stops a graph somebody left lying around for a month from turning
#: every board read into a full table scan.
MAX_WIRE_POSTS = 2000

#: The most live edges or live watches one read will consider. The live set is
#: bounded by design — an edge exists only while something is actually waiting,
#: and one landing clears a batch of them — so this is not expected to bind. It
#: exists because the read is a graph closure rather than a row filter, and the
#: honest failure for a graph that has grown past what a closure can carry is to
#: say `truncated` rather than to return a page and let it read as the whole.
MAX_LIVE_ROWS = 5000

#: What a node may be. Closed, and adding to it is a code change for the reason
#: :data:`app.claimkey.REF_KINDS` is closed: every kind needs a key shape that
#: cannot collide with another kind's. A branch is deliberately absent — landing
#: is about issues and pull requests, and ``<repo>:<branch>`` is the *merge*
#: claim's namespace, not this one.
NODE_KINDS = ("issue", "pr")

#: The number half of a node ref, as a string, before :func:`app.claimkey.derive`
#: turns it into a key. Permissive on the sigil (``#293`` and ``293`` are one
#: pull request) and strict on everything else, so a caller that sends a title by
#: mistake gets a 422 rather than a node nobody can ever match.
_NUMBER_RE = re.compile(r"\A#?\d{1,12}\Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NodeRef(BaseModel):
    """One end of an edge, or the node being minded.

    ``repo`` is required and is never inherited from the other end of an edge.
    That is the cross-repository story in one field: the motivating case is
    ``nix-fleet#40`` waiting on ``quarterback#290``, and a design where the second
    repository defaults to the first gets that case wrong by default and right
    only when somebody remembers. The MCP tools fill it from
    ``remote.origin.url`` per side, so an agent still types it only when the ends
    differ.
    """

    kind: Literal["issue", "pr"]
    value: str = Field(min_length=1, max_length=16,
                       description="the issue or pull request number")
    repo: str = Field(min_length=1, max_length=140, description="owner/name")


class GateIn(BaseModel):
    blocked: NodeRef
    blocker: NodeRef
    note: str | None = Field(default=None, max_length=500,
                             description="why — the half of an edge no structure recovers")


class ClearIn(BaseModel):
    blocker: NodeRef
    blocked: NodeRef | None = Field(
        default=None, description="omit to clear everything this blocker was gating")
    resolution: str = Field(description="`landed` or `dropped` — they are different facts")


class MindIn(BaseModel):
    node: NodeRef
    note: str | None = Field(default=None, max_length=500,
                             description="what you are waiting to do")
    session: str | None = Field(default=None, max_length=200,
                                description="your session, so the watch lives while you do")
    ttl: int | None = Field(default=None, ge=60, le=MAX_WATCH_TTL,
                            description="backstop only; presence is the real expiry")


class UnmindIn(BaseModel):
    node: NodeRef
    holder: str | None = Field(default=None, max_length=200,
                               description="whose watch; defaults to yours")


def _key(ref: NodeRef, field: str) -> tuple[str, str, str]:
    """``(key, repo, value)`` for one end — derived, never composed (#172)."""
    if not _NUMBER_RE.match(ref.value.strip()):
        raise HTTPException(422, detail={
            "error": f"{ref.value!r} is not an issue or pull request number",
            "field": field,
            "hint": "a node is a number: `293` or `#293`. The title goes in `note`.",
        })
    try:
        repo = canonical_repo(ref.repo)
        _, key = derive(ref.kind, repo=repo, value=ref.value)
    except BadRef as e:
        raise HTTPException(422, detail={"error": str(e), "field": field,
                                         "repo": ref.repo}) from None
    return key, repo, ref.value.strip().lstrip("#")


def _node_view(key: str, repo: str, kind: str, value: str) -> dict:
    return {"key": key, "repo": repo, "kind": kind, "value": value}


def _parse_key(key: str) -> dict:
    """A node key back into its parts, for rendering. Total: never raises.

    The key is the identity and the parts are a convenience for whoever draws the
    row — the static pages build a github.com URL from ``repo``/``kind``/``value``
    and there is no URL column here for the same reason there is none in the plan
    (``app/static/board.html``'s ``refUrl``).
    """
    for sigil, kind in (("#", "issue"), ("!", "pr")):
        repo, _, number = key.rpartition(sigil)
        if repo and number.isdigit():
            return _node_view(key, repo, kind, number)
    return _node_view(key, "", "", "")


def _edge_side(key: str, edge: LandingEdge, landed: dict[str, Any] | None) -> dict:
    """One edge, rendered from whichever end the reader is standing at."""
    return {
        **_parse_key(key),
        "note": edge.note,
        "asserted_by": edge.asserted_by,
        "since": edge.created_at.isoformat(),
        # Not a resolution — evidence. The sweep has already acted on anything it
        # was allowed to act on, so a `landed` still attached to a LIVE edge means
        # the board saw an announcement it could not attribute confidently (a
        # bare repository name); it is shown so a human can clear it in one call
        # rather than wondering why the edge is still there.
        "landed": None if landed is None else {
            "post": landed["id"], "ts": landed["ts"].isoformat(),
            "summary": landed["summary"],
        },
    }


def _watch_view(w: LandingWatch) -> dict:
    return {
        "holder": w.holder,
        "session": w.session,
        "note": w.note,
        "since": w.created_at.isoformat(),
        "renewed": w.renewed_at.isoformat(),
        "expires": w.expires_at.isoformat(),
    }


def _repo_match(repo: str):
    """A predicate over a stored ``owner/name``, however the caller spelled it.

    The two-tier rule ``GET /worktrees`` settled on: ``owner/name`` is folded and
    compared exactly, a **bare** name is compared by basename, and anything that
    is neither is a 422 rather than an empty answer — an empty answer reads as
    "nothing gates anything here" when it means "I could not tell what you asked
    about", which is the false-clean #326 is about.

    A predicate rather than a SQL ``WHERE`` because the scope of this read is a
    graph closure rather than a row filter — see :func:`read_graph`. One
    implementation either way, so the two tiers cannot come to disagree.
    """
    try:
        want = canonical_repo(repo)
        return lambda stored: stored == want
    except BadRef:
        pass
    asked = repo.strip()
    if not REPO_NAME_RE.match(asked):
        raise HTTPException(422, detail={"error": REPO_SHAPE, "repo": repo})
    bare = repo_key(asked)
    return lambda stored: repo_key(stored) == bare


async def _live_edges(session: AsyncSession) -> list[LandingEdge]:
    """Every unresolved edge. Whole, and the scope is applied above this.

    A row filter cannot answer a scoped question here. ``?repo=quarterback`` has
    to return the chain ``quarterback!290 → nix-fleet#23 → nix-fleet!31``, not
    its first hop — stopping at the boundary would report ``nix-fleet#23`` as
    landable when three things gate it, which is a confident wrong answer about
    exactly the cross-repository case this primitive exists for.

    Reading the live set whole is what :func:`app.api.plan.read_plan` does with
    the open items, for the same reason: the live set is bounded by design (an
    edge exists only while something is actually waiting, and every landing
    clears a batch) while *history* is what grows, and history is excluded by
    ``resolved_at IS NULL`` at the index.
    """
    return list(await session.scalars(
        select(LandingEdge).where(LandingEdge.resolved_at.is_(None))
        .order_by(LandingEdge.created_at, LandingEdge.id).limit(MAX_LIVE_ROWS + 1)))


async def _live_watches(session: AsyncSession) -> list[LandingWatch]:
    return list(await session.scalars(
        select(LandingWatch).where(LandingWatch.released_at.is_(None))
        .order_by(LandingWatch.created_at, LandingWatch.id).limit(MAX_LIVE_ROWS + 1)))


async def _wire(session: AsyncSession, since: datetime | None) -> list[dict[str, Any]]:
    """The merge announcements the board already holds, newest-first, capped.

    ``ix_posts_type`` makes the type filter cheap; ``since`` is the oldest live
    edge, because an announcement older than every edge cannot resolve one.
    """
    stmt = select(Post.id, Post.ts, Post.type, Post.summary, Post.refs).where(
        Post.type == LANDING_POST_TYPE)
    if since is not None:
        stmt = stmt.where(Post.ts >= since)
    rows = (await session.execute(stmt.order_by(Post.ts.desc(), Post.id.desc())
                                  .limit(MAX_WIRE_POSTS))).all()
    return [{"id": r.id, "ts": r.ts, "type": r.type, "summary": r.summary, "refs": r.refs}
            for r in rows]


async def _presence(session: AsyncSession, keys: set[str],
                    now: datetime) -> tuple[set[str], set[str]]:
    """``(sessions we have ever seen, sessions present right now)`` — one query.

    Both halves, because "gone" is a different fact from "never here". A watch
    whose session once held a lease and now holds none is a watcher that
    vanished, and sweeping it is the whole design. A watch whose session this
    board has never seen a lease for is not evidence of anything — a scripted
    watcher, or an agent whose lifecycle hook is not wired up — and sweeping it
    on the first read would delete a legitimate watch for a reason its holder
    could not see. That one falls back to its TTL, and ``POST /landing/mind``
    says so on the way in.
    """
    if not keys:
        return set(), set()
    rows = (await session.execute(
        select(Lease.session, func.bool_or(
            and_(Lease.released_at.is_(None), Lease.expires_at > now)))
        .where(Lease.session.in_(keys)).group_by(Lease.session))).all()
    ever = {r[0] for r in rows}
    live = {r[0] for r in rows if r[1]}
    return ever, live


async def _over(session: AsyncSession, edges: list[LandingEdge],
                watches: list[LandingWatch], wire: list[dict[str, Any]],
                now: datetime) -> tuple[dict[uuid.UUID, int], set[uuid.UUID]]:
    """What the board can already see is over — decided, not yet written.

    ``({edge id: the post that landed it}, {watch ids that have lapsed})``. Pure
    with respect to the graph: nothing the caller sent reaches this, which is
    what makes it safe to run for a reader who may not write (see
    :func:`read_graph`).
    """
    landed = announced_merges(wire)
    resolved = {e.id: landed[e.blocker_key]["id"] for e in edges
                if e.blocker_key in landed}

    ever, live = await _presence(session, {w.session for w in watches if w.session}, now)
    lapsed = {w.id for w in watches
              if w.expires_at <= now
              or (w.session is not None and w.session in ever and w.session not in live)}
    return resolved, lapsed


async def _sweep(session: AsyncSession, resolved: dict[uuid.UUID, int],
                 lapsed: set[uuid.UUID], now: datetime) -> dict[str, int]:
    """Write down what :func:`_over` decided. Passive, guarded, and it commits.

    **Every write is conditioned on the row still being live**, and that is not
    belt-and-braces. The rows were read some milliseconds ago; between then and
    now a peer may have cleared an edge as ``dropped`` or stood a watch down
    deliberately. An unconditional ORM assignment would overwrite both — turning
    "somebody decided this constraint was mistaken" into "it landed", and "the
    watcher finished waiting" into "the watcher vanished". Those are precisely
    the two distinctions this table exists to keep, so the sweep must lose that
    race rather than win it, and a guarded UPDATE is how it does.

    Statement-level updates rather than attribute assignment for the same reason:
    an ORM instance mutated here would flush whatever it holds, guard or no
    guard.
    """
    swept = {"edges": 0, "watches": 0}
    for edge_id, post_id in resolved.items():
        result = await session.execute(
            update(LandingEdge)
            .where(LandingEdge.id == edge_id, LandingEdge.resolved_at.is_(None))
            .values(resolved_at=now, resolved_by=f"board:post/{post_id}",
                    resolution="landed"))
        swept["edges"] += result.rowcount or 0
    for watch_id in lapsed:
        result = await session.execute(
            update(LandingWatch)
            .where(LandingWatch.id == watch_id, LandingWatch.released_at.is_(None))
            .values(released_at=now, lapsed=True))
        swept["watches"] += result.rowcount or 0
    if swept["edges"] or swept["watches"]:
        await session.commit()
    return swept


async def _claims_for(session: AsyncSession, keys: set[str],
                      now: datetime) -> dict[str, ResourceLease]:
    """The live claim on each node, in one query — the same key, the same table.

    A node's claim and its minders answer two different questions and the graph
    shows both: ``claim`` is *somebody is doing this now*, ``minders`` is
    *somebody is waiting for it*. Claiming work you cannot start blocks it for
    everyone while nothing happens, so "watched and unclaimed" is a legitimate
    and common state — and one a reader must be able to see, or it will hand the
    node to the next agent that asks.
    """
    if not keys:
        return {}
    rows = await session.scalars(
        select(ResourceLease).where(ResourceLease.kind == WORK,
                                    ResourceLease.key.in_(keys),
                                    ResourceLease.released_at.is_(None),
                                    ResourceLease.expires_at > now))
    return {r.key: r for r in rows}


@router.post("/landing/gate", status_code=201)
async def assert_gate(
    body: GateIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record that one node cannot land until another has. Both ends name their repo.

    Idempotent: asserting a live edge again updates its note and returns
    ``created: false``, because a fact somebody states twice is one fact. The
    unique index over live rows is what makes that atomic rather than a race
    between a SELECT and an INSERT.

    **This refuses nothing about the shape of the graph.** A cycle is accepted and
    reported (see :func:`read_graph`): two pull requests that each genuinely need
    the other first is a real deadlock a human has to break, and a store that
    will not hold the fact leaves it where #294 found it — in prose, in a board
    post nothing queries. The one thing the table refuses is a self-edge, which
    is a client bug rather than a graph.
    """
    blocked_key, blocked_repo, _ = _key(body.blocked, "blocked")
    blocker_key, blocker_repo, _ = _key(body.blocker, "blocker")
    if blocked_key == blocker_key:
        raise HTTPException(422, detail={
            "error": "a node cannot gate itself",
            "node": blocked_key,
            "hint": "the two ends of an edge are different nodes; check the numbers",
        })

    now = _utcnow()
    # Two ways to lose a race here and they need different answers, so the whole
    # thing loops rather than branching once. Renewing an edge a peer resolves in
    # the same instant must NOT report a live edge that is not there — the update
    # is guarded on `resolved_at IS NULL` and a miss falls through to insert a
    # fresh one, which is what the caller actually asked for. Inserting one a
    # peer creates first is not an error at all: the fact exists, which is all
    # anybody wanted.
    for _ in range(3):
        existing = await session.scalar(
            select(LandingEdge).where(LandingEdge.blocked_key == blocked_key,
                                      LandingEdge.blocker_key == blocker_key,
                                      LandingEdge.resolved_at.is_(None)))
        if existing is not None:
            values = {"updated_at": now}
            if body.note is not None:
                values["note"] = body.note
            result = await session.execute(
                update(LandingEdge)
                .where(LandingEdge.id == existing.id, LandingEdge.resolved_at.is_(None))
                .values(**values))
            await session.commit()
            if result.rowcount:
                refreshed = await session.get(LandingEdge, existing.id)
                return {**_edge_row(refreshed), "created": False,
                        "advice": _advice(refreshed)}
            continue  # it resolved underneath us; assert it again

        edge = LandingEdge(
            blocked_key=blocked_key, blocked_repo=blocked_repo,
            blocker_key=blocker_key, blocker_repo=blocker_repo,
            note=body.note, asserted_by=author, created_at=now, updated_at=now)
        session.add(edge)
        try:
            await session.commit()
        except IntegrityError as e:
            await session.rollback()
            if not is_unique_violation(e):
                raise
            continue  # a peer got there first; go and read theirs
        return {**_edge_row(edge), "created": True, "advice": _advice(edge)}

    # Three rounds of losing to a peer that is both creating and resolving this
    # exact edge. Saying so beats a fourth attempt or a fabricated success.
    raise HTTPException(409, detail={
        "error": "this edge is being created and resolved at the same time",
        "blocked": blocked_key, "blocker": blocker_key,
        "hint": "a peer is contending on it right now — read `GET /landing` and retry",
    })


def _edge_row(edge: LandingEdge) -> dict:
    return {
        "edge_id": str(edge.id),
        "blocked": _parse_key(edge.blocked_key),
        "blocker": _parse_key(edge.blocker_key),
        "note": edge.note,
        "asserted_by": edge.asserted_by,
        "since": edge.created_at.isoformat(),
        "updated": edge.updated_at.isoformat(),
        "resolved": None if edge.resolved_at is None else {
            "at": edge.resolved_at.isoformat(),
            "by": edge.resolved_by,
            "resolution": edge.resolution,
        },
        "cross_repo": edge.blocked_repo != edge.blocker_repo,
    }


def _advice(edge: LandingEdge) -> str | None:
    """Where a same-repo issue-to-issue edge actually belongs (#229).

    Said rather than enforced. Refusing it would push the fact back into prose,
    which is the failure this primitive exists to end; saying nothing would leave
    a second store of something GitHub owns growing quietly. So the edge is kept
    and the response names the better home for it every single time.
    """
    if (edge.blocked_repo == edge.blocker_repo
            and edge.blocked_key.rpartition("#")[1] == "#"
            and edge.blocker_key.rpartition("#")[1] == "#"):
        return ("both ends are issues in one repository, which is a dependency "
                "GitHub's own graph holds natively (#229) — `gh` it there too, and "
                "prefer it as the origin. This board keeps what GitHub cannot: "
                "edges across repositories, edges ending on a pull request, and "
                "who is minding a node.")
    return None


@router.post("/landing/clear")
async def clear_gate(
    body: ClearIn,
    author: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Say an edge is over — because the blocker landed, or because it was wrong.

    ``blocker`` alone clears everything that node was gating, which is the shape
    the fact actually arrives in: one landing frees every downstream node at once
    (PR #293 unblocked three), and making the caller enumerate them is how one
    gets missed.

    ``resolution`` is required and has two values on purpose. *landed* says the
    work happened; *dropped* says somebody decided the constraint was mistaken.
    A store that collapsed them could not tell a reader a fortnight later which
    of those two very different things occurred.
    """
    if body.resolution not in RESOLUTIONS:
        raise HTTPException(422, detail={
            "error": f"{body.resolution!r} is not a resolution",
            "resolutions": list(RESOLUTIONS),
            "hint": "`landed` if the blocker merged, `dropped` if the edge was wrong",
        })
    blocker_key, _, _ = _key(body.blocker, "blocker")
    stmt = select(LandingEdge).where(LandingEdge.blocker_key == blocker_key,
                                     LandingEdge.resolved_at.is_(None))
    if body.blocked is not None:
        blocked_key, _, _ = _key(body.blocked, "blocked")
        stmt = stmt.where(LandingEdge.blocked_key == blocked_key)
    rows = list(await session.scalars(stmt))
    now = _utcnow()
    for edge in rows:
        edge.resolved_at = now
        edge.resolved_by = author
        edge.resolution = body.resolution
    if rows:
        await session.commit()
    return {"blocker": _parse_key(blocker_key), "resolution": body.resolution,
            "cleared": len(rows), "edges": [_edge_row(e) for e in rows]}


@router.post("/landing/mind", status_code=201)
async def mind_node(
    body: MindIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stand by for a node, visibly. Not a claim, and it must not be read as one.

    Minding says *I am waiting for this*; claiming says *I am doing this*. They
    are different states and the graph shows both, because "watched and
    unclaimed" is the correct thing to be when the work cannot start yet —
    claiming what you cannot begin blocks it for everybody while nothing happens.
    Several agents may mind one node; exactly one may claim it.

    Pass your ``session`` and the watch lives as long as your presence does. That
    is the whole expiry design: a fixed TTL is wrong because a three-day wait is
    legitimate, and no TTL is worse because a session that dies at 2am would
    leave a watch that reads as somebody standing by. Renewal on presence gets
    the dead-session case right for free — the lifecycle hook is already renewing
    the lease, so a watcher does nothing at all to keep this alive.
    """
    node_key, node_repo, _ = _key(body.node, "node")
    now = _utcnow()
    ttl = body.ttl or DEFAULT_WATCH_TTL
    existing = await session.scalar(
        select(LandingWatch).where(LandingWatch.node_key == node_key,
                                   LandingWatch.holder == holder,
                                   LandingWatch.released_at.is_(None)))
    renewed = existing is not None
    if existing is not None:
        watch = existing
        watch.session = clean_session(body.session) or watch.session
        if body.note is not None:
            watch.note = body.note
        watch.ttl_seconds = ttl
        watch.renewed_at = now
        watch.expires_at = now + timedelta(seconds=ttl)
    else:
        watch = LandingWatch(
            node_key=node_key, node_repo=node_repo, holder=holder,
            session=clean_session(body.session), note=body.note, ttl_seconds=ttl,
            created_at=now, renewed_at=now, expires_at=now + timedelta(seconds=ttl))
        session.add(watch)
    await session.commit()

    others = list(await session.scalars(
        select(LandingWatch).where(LandingWatch.node_key == node_key,
                                   LandingWatch.holder != holder,
                                   LandingWatch.released_at.is_(None),
                                   LandingWatch.expires_at > now)))
    return {
        "watch_id": str(watch.id),
        "node": _parse_key(node_key),
        **_watch_view(watch),
        "renewed": renewed,
        # The reason this endpoint answers with the peers rather than just an ok:
        # two agents minding one node without knowing it is the failure #294
        # opens with, and it took a human pasting one message into the other's
        # session to fix. Now the second one is told on the way in.
        "also_minding": [_watch_view(w) for w in others],
        "presence": ("this watch lives while your session does"
                     if watch.session else
                     "no session given, so this watch has only its TTL — pass "
                     "`session` and it expires when you do"),
    }


@router.post("/landing/unmind")
async def unmind_node(
    body: UnmindIn,
    holder: str = Depends(identify),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stop standing by (idempotent). ``lapsed`` stays false: you let go, you did not vanish."""
    node_key, _, _ = _key(body.node, "node")
    whose = body.holder or holder
    watch = await session.scalar(
        select(LandingWatch).where(LandingWatch.node_key == node_key,
                                   LandingWatch.holder == whose,
                                   LandingWatch.released_at.is_(None)))
    if watch is None:
        return {"node": _parse_key(node_key), "holder": whose, "released": False}
    if not same_machine(watch.holder, holder):
        raise HTTPException(403, detail={
            "error": "not your watch",
            "node": node_key,
            "held_by": watch.holder,
            "hint": ("a watch belongs to the agent that set it. If it is stale, "
                     "its holder's presence lapsing will clear it on the next read."),
        })
    watch.released_at = _utcnow()
    await session.commit()
    return {"node": _parse_key(node_key), "holder": whose, "released": True,
            **_watch_view(watch)}


def _reachable(seeds: set[str], blockers: dict[str, set[str]],
               blocks: dict[str, set[str]]) -> set[str]:
    """Every node connected to a seed by live edges, in either direction.

    **The closure, not the first hop**, and this is the correctness of a scoped
    read rather than a nicety. ``?repo=quarterback`` over
    ``quarterback!290 → nix-fleet#23 → nix-fleet!31`` truncated at the boundary
    would report ``nix-fleet#23`` with no blockers — ``landable: true``,
    ``depth: 0`` — when three things gate it, and would take a cycle that leaves
    the repository and comes back for an ordinary chain. A confident wrong answer
    about the cross-repository case is the one failure this primitive must not
    have, since crossing repositories is the whole reason it exists.

    Both directions, because both halves are load-bearing: downstream tells you
    what is still in your way, upstream tells you what landing this would free.
    """
    reached, pending = set(seeds), list(seeds)
    while pending:
        key = pending.pop()
        for nxt in blockers.get(key, ()) | blocks.get(key, ()):
            if nxt not in reached:
                reached.add(nxt)
                pending.append(nxt)
    return reached


@router.get("/landing")
async def read_graph(
    repo: str | None = Query(default=None,
                             description="`owner/name`, or the bare name. Scope is the "
                                         "CLOSURE from this repo's nodes, so a chain that "
                                         "leaves it comes back whole — that is the point "
                                         "of a cross-repo graph."),
    node: str | None = Query(default=None,
                             description="one node's key, e.g. `prisonblues/quarterback!290`, "
                                         "and everything its chain reaches"),
    session: AsyncSession = Depends(get_session),
    reader_name: str = Depends(reader),
    bearer: str | None = Depends(optional_identity),
) -> dict:
    """The graph: every node with a live edge or a live minder, and what it is waiting on.

    Read this cold and you get, per node: what still gates it, what landing it
    would unblock, how many landings it is from being landable, how many merges
    have gone past it while it waited, who is minding it and who is doing it.

    It **decides nothing**. There is no order in the response, no
    recommendation, and no ``next``: turning this and #94's file overlap into a
    suggested merge order is #80's half of the problem and it consumes this rather
    than living in it.

    ## Scope is a closure, and `in_scope` says which nodes you asked for

    ``?repo=`` seeds on that repository's nodes and then follows the chain
    wherever it goes, so a blocker two hops away in another repository is in the
    answer with its own blockers intact. Nodes reached that way carry
    ``in_scope: false`` — they are context rather than your list, and a renderer
    may grey them — but their `depth` and `landable` are computed from the whole
    chain, because a number computed from half a chain is worse than none.

    ## An edge that is over is filtered for everyone and written down by agents

    Whether the caller may WRITE decides only whether the sweep is *persisted*,
    never what the answer says. :func:`_over` is pure with respect to the request
    — nothing the caller sent reaches it — so a browser gets the same graph, with
    the finished rows filtered out of its view and left in the table for the next
    agent read to clear.

    That split is :func:`app.auth.reader`'s own rule, honoured rather than
    quietly widened. A ``Remote-User`` here is asserted by the edge and, as that
    docstring says, a spoofed one "buys a caller a *read* of a board every
    enrolled agent can already read" — so it must not also buy a committed write.
    Persisting on the bearer path alone costs nothing in freshness: every
    ``landing_graph`` call an agent makes is bearer-authenticated, and they are
    the overwhelming majority of reads.
    """
    now = _utcnow()
    match = _repo_match(repo) if repo is not None else None
    wanted = node.strip().lower() if node else None

    edges = await _live_edges(session)
    watches = await _live_watches(session)
    truncated = len(edges) > MAX_LIVE_ROWS or len(watches) > MAX_LIVE_ROWS
    edges, watches = edges[:MAX_LIVE_ROWS], watches[:MAX_LIVE_ROWS]

    oldest = min([e.created_at for e in edges], default=None)
    wire = await _wire(session, oldest)

    resolved, lapsed = await _over(session, edges, watches, wire, now)
    # Filtered for every caller; written down only by one that may write.
    edges = [e for e in edges if e.id not in resolved]
    watches = [w for w in watches if w.id not in lapsed]
    if bearer is not None and (resolved or lapsed):
        swept = await _sweep(session, resolved, lapsed, now)
    else:
        swept = {"edges": 0, "watches": 0}
    swept["persisted"] = bearer is not None
    swept["over"] = len(resolved) + len(lapsed)

    blockers, blocks = adjacency([(e.blocked_key, e.blocker_key) for e in edges])
    for watch in watches:
        blockers.setdefault(watch.node_key, set())
        blocks.setdefault(watch.node_key, set())

    if match is None and wanted is None:
        seeds = set(blockers)
    else:
        seeds = {k for k in blockers
                 if (wanted is not None and k == wanted)
                 or (match is not None and match(_parse_key(k)["repo"]))}
    scope = _reachable(seeds, blockers, blocks) if (match or wanted) else set(blockers)
    blockers = {k: v & scope for k, v in blockers.items() if k in scope}
    blocks = {k: v & scope for k, v in blocks.items() if k in scope}
    edges = [e for e in edges
             if e.blocked_key in scope and e.blocker_key in scope]
    watches = [w for w in watches if w.node_key in scope]

    depth = depths(blockers)
    loops = cycles(blockers)
    stuck = {n for loop in loops for n in loop}
    landed = announced_merges(wire)
    by_pair = {(e.blocked_key, e.blocker_key): e for e in edges}
    minders: dict[str, list[LandingWatch]] = {}
    for watch in watches:
        minders.setdefault(watch.node_key, []).append(watch)
    claims = await _claims_for(session, set(blockers), now)

    first_seen: dict[str, datetime] = {}
    for edge in edges:
        for key in (edge.blocked_key, edge.blocker_key):
            first_seen[key] = min(first_seen.get(key, edge.created_at), edge.created_at)
    for watch in watches:
        key = watch.node_key
        first_seen[key] = min(first_seen.get(key, watch.created_at), watch.created_at)

    nodes = []
    for key in sorted(blockers):
        parts = _parse_key(key)
        claim = claims.get(key)
        mine = minders.get(key, [])
        nodes.append({
            **parts,
            "blocked_by": [_edge_side(b, by_pair[(key, b)], landed.get(b))
                           for b in sorted(blockers.get(key, ()))
                           if (key, b) in by_pair],
            "blocks": [_edge_side(d, by_pair[(d, key)], landed.get(key))
                       for d in sorted(blocks.get(key, ()))
                       if (d, key) in by_pair],
            "landable": not blockers.get(key),
            "depth": depth.get(key),
            "in_cycle": key in stuck,
            # What you asked for, versus what its chain dragged in. Said out loud
            # rather than left to be worked out by comparing repos, because a
            # node reached through a cycle can be in your repo AND not a seed.
            "in_scope": key in seeds if (match or wanted) else True,
            "minders": [_watch_view(w) for w in mine],
            "minded": bool(mine),
            "since": first_seen[key].isoformat() if key in first_seen else None,
            "passed_by": landings_since(wire, parts["repo"], first_seen.get(key)),
            "claim": None if claim is None else {
                "holder": claim.holder, "session": claim.session, "note": claim.note,
                "expires": claim.expires_at.isoformat(),
            },
        })

    blocked = [n for n in nodes if not n["landable"]]
    return {
        "repo": repo,
        "node": node,
        "generated": now.isoformat(),
        "read_by": reader_name,
        "nodes": nodes,
        # Named rather than refused — see `assert_gate`. A cycle is a real
        # deadlock somebody has to break, and `depth` is null for its members
        # because there is no honest distance to publish.
        "cycles": loops,
        "swept": swept,
        # A page of a graph is a different graph, so a reader is told rather than
        # left to infer it from a count that looks complete.
        "truncated": truncated,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "watches": len(watches),
            "blocked": len(blocked),
            "landable": sum(1 for n in nodes if n["landable"]),
            "minded": sum(1 for n in nodes if n["minded"]),
            # The state #294 is actually about: gated, and nobody standing by.
            # A count, not a verdict — what to do about it is the reader's.
            "blocked_unminded": sum(1 for n in blocked if not n["minded"]),
            "in_cycle": len(stuck),
        },
    }
