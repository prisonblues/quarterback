from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ResourceLease(Base):
    """An atomic claim on a NAMED RESOURCE, held by one agent at a time.

    ``Lease`` is the lock half of session sync and is keyed on a session; this is
    the same passive-expiry design applied to anything else two agents can want
    at once. Every kind comes off this one table, which is the point — #99, #46,
    the plan and the checkout each wanted an atomic claim, and two independent
    implementations of "who has this right now" is the outcome to avoid:

    * ``kind="work"`` — a unit of work: an issue (``<repo>#172``), a PR
      (``<repo>!207``), a plan (``plan:<uuid>``), a plan item. **The key is
      derived from the resource and never composed by the caller** — that rule is
      :mod:`app.claimkey`, and #172 is what happens without it: two spellings of
      one collision are two resources by construction, because ``(kind, key)`` is
      the unique index.
    * ``kind="merge"``, ``key="<repo>:<branch>"`` — held across a land.

    **There is no release kind here any more (#172).** ``kind="release"``,
    ``key="<repo>:<version>"`` used to be listed above as a thing this table
    carried, and it was the one documented kind nothing could write: the
    allocator, ``POST /release/claim``, ``POST /release/reclaim``, ``GET
    /releases`` and their tools are all deleted. ``scripts/release_stamp.py``
    (v2.34) is the whole mechanism — it takes ``max+1`` at land from the ref being
    merged into, which is a question a git ref answers on its own. Nine releases
    landed that way in a day with no collisions while the allocator's own rows
    went stale for every PR still open, and a stale record of a claim nobody takes
    is worse than no record: it is a second answer to a question that has one.
    Rows with that kind may survive in an old deployment's history, and that is
    all they are.

    **Advisory, and it must never be described otherwise.** The board cannot gate
    github.com: a human merging in the UI, or any agent not enrolled here, lands
    regardless. What this removes is collisions between agents that DO ask, which
    is the observed failure mode and the whole claim. The correctness backstop
    stays where it was — the pre-land verdict re-checked after base movement, and
    CI on ``main``. A skill describing this as "the merge lock" is wrong.

    **Expiry is passive**, exactly as :class:`Lease` gets right: a lease is active
    while ``released_at IS NULL AND expires_at > now()``, and a crashed holder
    never renews. No reaper — which matters more here than for sessions, because
    a wedged claim on ``main`` would block everybody's landing rather than one
    agent's own handoff.

    Atomicity is the unique index, not a read-then-write. ``ix_resource_leases_held``
    is UNIQUE on ``(kind, key)`` over unreleased rows only, so a second claimant
    loses at the database rather than by losing a race between its own SELECT and
    INSERT. It cannot be conditioned on ``expires_at > now()`` — a partial index
    predicate has to be immutable — so the claim path sweeps a lapsed row into
    ``released_at`` first and then inserts. That sweep is still passive: it runs
    only when somebody asks for that exact key.
    """

    __tablename__ = "resource_leases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: What sort of resource this is. Free text rather than an enum type: a third
    #: kind should cost an endpoint and not a migration — the same argument
    #: ``review_findings.provenance`` records. Which kinds a caller may actually
    #: spell is decided above the table, in :mod:`app.claimkey`, because a column
    #: type cannot fold two spellings of one resource onto one row.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: The resource itself: ``<repo>#172``, ``<repo>!207``, ``plan:<uuid>``,
    #: ``<repo>:<branch>``. Derived from what is being claimed rather than
    #: composed by whoever is claiming it (:mod:`app.claimkey`) — a key a caller
    #: types is a key the next caller types differently. A shape this board does
    #: not recognise is left exactly as sent: the namespace is open on purpose.
    key: Mapped[str] = mapped_column(Text, nullable=False)
    holder: Mapped[str] = mapped_column(Text, nullable=False)  # token name (see auth.identify)
    #: Which session is holding it, when the caller says. Not the identity — that
    #: is ``holder`` — but the thing a peer needs to reach whoever is landing.
    session: Mapped[str | None] = mapped_column(Text)
    #: One line on what the holder is doing with it, shown to whoever is refused.
    #: "Held" with no why is an obstruction; "held, landing #128" is coordination.
    note: Mapped[str | None] = mapped_column(Text)
    ttl_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: TRUE when ``released_at`` was set by the TTL sweep rather than by the
    #: holder letting go. Two different facts: one agent finished, the other
    #: vanished. Collapsing them would make an abandoned claim indistinguishable
    #: from a completed one — "the branch landed" and "whoever was landing it
    #: stopped answering" are different things to read off a dashboard, and
    #: ``qbdata`` filters on this column to tell them apart.
    lapsed: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)

    __table_args__ = (
        # The atomicity. UNIQUE over unreleased rows only, so history accumulates
        # (who held this branch last, and whether they finished or lapsed, is a
        # question worth asking after the fact) while at most one claim on a key
        # can be outstanding.
        Index("ix_resource_leases_held", "kind", "key", unique=True,
              postgresql_where=text("released_at IS NULL")),
        Index("ix_resource_leases_kind_key", "kind", "key"),
        Index("ix_resource_leases_holder", "holder"),
    )
