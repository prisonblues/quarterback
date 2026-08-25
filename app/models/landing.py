from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: Why an edge stopped being live. Two facts, never one: the blocker landed, or
#: somebody decided the edge was wrong. Collapsing them would make the graph's own
#: history unreadable — "this edge is gone" cannot tell you whether the work
#: happened or whether the constraint was mistaken, and those call for opposite
#: reactions from whoever reads it next.
#:
#: The check constraint below is built from this tuple rather than restating it,
#: so the declared vocabulary and the enforced one cannot drift — the rule
#: :data:`app.models.plan_item.STATES` follows for the same reason.
RESOLUTIONS = ("landed", "dropped")

_RESOLUTION_LIST = ", ".join(f"'{r}'" for r in RESOLUTIONS)


class LandingEdge(Base):
    """One gating fact: this node cannot land until that one has — #294.

    ``nix-fleet#40`` waited on ``quarterback#290``, ``nix-fleet#23``, ``#31`` and
    ``#32``. One issue, four blockers, two repositories, and the only place any of
    it was written down was prose in a board post's detail tier and a markdown
    file on an unpushed branch. Nothing queried it, so no agent picking up #290
    could learn that another repository's step 0 was behind it.

    **A node is a claim key, and that is the whole of the cross-repo story.**
    ``blocked_key`` and ``blocker_key`` come from :func:`app.claimkey.derive`, so
    they read ``prisonblues/nix-fleet#40`` and ``prisonblues/quarterback!290`` —
    fully qualified, with the repository inside the identity rather than beside
    it. An edge crosses repositories by construction; there is no same-repo fast
    path for it to fall off, and no column anywhere that means "the repo we are
    talking about". It is also *the very key the claim table uses*, so "who is
    working on this node" and "what gates it" join without a translation layer
    (#99: two implementations of one question is the outcome to avoid).

    **Fan-out costs nothing extra.** PR #293 closed #177 and #259 and unblocked
    #188 — one node, three outbound edges. Rows keyed on ``blocker_key`` are the
    fan-out and rows keyed on ``blocked_key`` are the fan-in; they are the same
    rows read from the two ends, which is why there is one table and not two.

    **It is a fact store and it decides nothing.** Nothing here merges, reorders
    or triggers. #229's line is held: an issue-to-issue dependency inside one
    repository is GitHub's fact and belongs in GitHub's native graph. What this
    holds is what GitHub cannot — an edge whose ends are in two repositories, an
    edge whose end is a *pull request*, and (in :class:`LandingWatch`) who is
    standing by for it.

    **Liveness is passive**, borrowed from
    :class:`~app.models.resource_lease.ResourceLease` because it is the same
    problem: an edge is live while ``resolved_at IS NULL``, history accumulates
    below that, and ``ix_landing_edges_live`` is UNIQUE over the live rows only —
    so asserting the same edge twice is a renew at the database rather than a
    second copy of one fact.
    """

    __tablename__ = "landing_edges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The node that cannot land yet — ``prisonblues/nix-fleet#40``. Derived, never
    #: composed by the caller: :mod:`app.claimkey` is the only maker of keys here
    #: for the reason #172 gives, which is that two spellings of one resource are
    #: two resources as far as an index is concerned.
    blocked_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: The repository half of ``blocked_key``, folded, stored beside it. Redundant
    #: on purpose: a scoped read (``GET /landing?repo=…``) is a column comparison
    #: rather than a ``LIKE`` against a key whose separator varies by kind.
    blocked_repo: Mapped[str] = mapped_column(Text, nullable=False)
    #: What it is waiting for — ``prisonblues/quarterback!290``.
    blocker_key: Mapped[str] = mapped_column(Text, nullable=False)
    blocker_repo: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why, in one line: *"all three touch files the upstream move deletes, so all
    #: three must land or be abandoned before the flake bump"*. The reason is the
    #: half of an edge that no amount of graph structure recovers, and it is the
    #: half that was living in a board post.
    #:
    #: A deadline is deliberately NOT a column. "These land before the bump" is
    #: already an edge — the bump is a node and the three are its blockers — so a
    #: date field would be a second, weaker spelling of a constraint the graph
    #: already holds exactly.
    note: Mapped[str | None] = mapped_column(Text)
    #: Who said so. An edge nobody will own is an edge nobody will retract.
    asserted_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Last re-asserted. Moves on an idempotent re-assert so a graph nobody has
    #: touched in a fortnight is visibly stale, the way a plan is.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The identity that cleared it, or ``board:post/<id>`` when the passive sweep
    #: cleared it from a ``published`` post already on the wire. Spelled out rather
    #: than left as a boolean, because "the board decided this on its own, from
    #: post 6146" is exactly what somebody disputing a resolution needs to read.
    resolved_by: Mapped[str | None] = mapped_column(Text)
    #: See :data:`RESOLUTIONS`.
    resolution: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # A node cannot gate itself. Not a hypothetical: the obvious client bug is
        # to pass one ref twice, and a self-edge is a node that is permanently
        # blocked with nothing to wait for — unfalsifiable, and it would poison
        # every depth walk that touched it.
        CheckConstraint("blocked_key <> blocker_key", name="ck_landing_edges_no_self"),
        CheckConstraint(f"resolution IS NULL OR resolution IN ({_RESOLUTION_LIST})",
                        name="ck_landing_edges_resolution"),
        # Resolved and why travel together or not at all: a row cleared with no
        # reason is the "this edge is gone but I cannot tell you what happened"
        # state the vocabulary exists to prevent.
        CheckConstraint("(resolved_at IS NULL) = (resolution IS NULL)",
                        name="ck_landing_edges_resolved_pair"),
        CheckConstraint(r"blocked_repo = lower(btrim(blocked_repo, E' \t\n\r\f\013'))",
                        name="ck_landing_edges_blocked_repo_canonical"),
        CheckConstraint(r"blocker_repo = lower(btrim(blocker_repo, E' \t\n\r\f\013'))",
                        name="ck_landing_edges_blocker_repo_canonical"),
        Index("ix_landing_edges_live", "blocked_key", "blocker_key", unique=True,
              postgresql_where=text("resolved_at IS NULL")),
        # The fan-out read: "what does landing this unblock?" — #293's three
        # outbound edges, answered from the blocker end.
        Index("ix_landing_edges_blocker", "blocker_key"),
        Index("ix_landing_edges_blocked", "blocked_key"),
        Index("ix_landing_edges_repos", "blocked_repo", "blocker_repo"),
    )


class LandingWatch(Base):
    """Who is minding a node — the half of #294 that is not an edge.

    A node with somebody standing by is a graph in motion; a node with nobody
    standing by is a stall nobody has noticed. Today those render identically,
    and the dangerous one is the second.

    ``cotton-oaken`` set a watch on three conditions in its own session, polling
    every 60 seconds. Correct, well specified, and **invisible**: another agent
    claimed the same work eight minutes later, unable to see that a peer was
    already standing by for exactly the artefact it was about to produce. What
    closed the loop was a human pasting one agent's message into the other's
    session — the brokering the board exists to remove.

    **A watch is not a claim, and must never be conflated with one.**
    :class:`~app.models.resource_lease.ResourceLease` is exclusive: UNIQUE on
    ``(kind, key)`` over live rows, one holder. Minding is the opposite shape —
    several agents may legitimately be waiting on the same PR, and none of them is
    *doing* it. Claiming work you cannot start blocks it for everybody while
    nothing happens, so the unique index here is on ``(node_key, holder)``: one
    live watch per agent per node, any number of agents per node.

    **Renewal on presence, not a fixed TTL.** A watch on a PR that takes three
    days to land is legitimate, so a short TTL would be wrong; a session that dies
    at 2am must not leave a watch that reads as somebody standing by, so no TTL at
    all would be worse. Both, then: a watch is live while it is unreleased, inside
    its own (generous) TTL, **and** its named session still holds an active
    :class:`~app.models.lease.Lease`. The lease is the fleet's existing liveness
    fact and it is already renewed by the lifecycle hook on every turn, so a watch
    tied to it costs its holder nothing to keep and expires the moment its holder
    stops existing.

    The sweep that records that is passive — no reaper, exactly as
    ``ResourceLease`` gets right — and it writes :attr:`lapsed`, so "the watcher
    finished waiting" and "the watcher vanished" stay two different facts. That
    distinction is the whole point: it is what makes *blocked and unattended*
    readable at all.
    """

    __tablename__ = "landing_watches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The node being minded, as a claim key — the same spelling
    #: :attr:`LandingEdge.blocked_key` uses, so a node's edges and its minders are
    #: one join and not a reconciliation.
    node_key: Mapped[str] = mapped_column(Text, nullable=False)
    node_repo: Mapped[str] = mapped_column(Text, nullable=False)
    #: The agent standing by — ``zeus/cotton-oaken``, the identity a peer can
    #: address a directed post to. That is the point of recording it: the failure
    #: this fixes was two agents unable to find each other.
    holder: Mapped[str] = mapped_column(Text, nullable=False)
    #: The session whose presence keeps this watch alive. NULL means the watch has
    #: only its own TTL to lean on — a scripted watcher with no lease — and it is
    #: reported as such rather than being quietly treated as live forever.
    session: Mapped[str | None] = mapped_column(Text)
    #: What they are waiting to do — *"landing #188 the moment #293 is in"*. A
    #: watch with no why is a name in a list; with one, it is somebody to talk to.
    note: Mapped[str | None] = mapped_column(Text)
    ttl_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    renewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: TRUE when the sweep ended it rather than the holder letting go — the TTL
    #: ran out, or the holder's session stopped being present. Same distinction,
    #: and same reason, as :attr:`app.models.resource_lease.ResourceLease.lapsed`.
    lapsed: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)

    __table_args__ = (
        CheckConstraint("ttl_seconds > 0", name="ck_landing_watches_ttl_positive"),
        CheckConstraint(r"node_repo = lower(btrim(node_repo, E' \t\n\r\f\013'))",
                        name="ck_landing_watches_repo_canonical"),
        Index("ix_landing_watches_live", "node_key", "holder", unique=True,
              postgresql_where=text("released_at IS NULL")),
        Index("ix_landing_watches_node", "node_key"),
        Index("ix_landing_watches_session", "session"),
        Index("ix_landing_watches_repo", "node_repo"),
    )
