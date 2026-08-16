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
    at once. Two kinds ship with it and both come off this one table, which is
    the point — #99 and #46 each wanted an atomic claim, and two independent
    implementations of "who has this right now" is the outcome to avoid:

    * ``kind="merge"``, ``key="<repo>:<branch>"`` — held across a land.
    * ``kind="release"``, ``key="<repo>:<version>"`` — held while a branch owns a
      release number.

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
    #: kind should cost an endpoint and not a migration, and the vocabulary is the
    #: caller's — the same argument ``review_findings.provenance`` records.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: The resource itself, namespaced by the caller (``<repo>:<branch>``).
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
    #: from a completed one — and for a release number that is the difference
    #: between "shipped" and "nobody knows", which is exactly what
    #: :func:`allocate_release` must not guess at.
    lapsed: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)

    __table_args__ = (
        # The atomicity. UNIQUE over unreleased rows only, so history accumulates
        # (an allocator needs every number ever handed out, not just the live
        # ones) while at most one claim on a key can be outstanding.
        Index("ix_resource_leases_held", "kind", "key", unique=True,
              postgresql_where=text("released_at IS NULL")),
        Index("ix_resource_leases_kind_key", "kind", "key"),
        Index("ix_resource_leases_holder", "holder"),
    )
