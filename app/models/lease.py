from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Lease(Base):
    """A TTL claim on a session by one device — the lock half of session sync.

    A lease is *active* while ``released_at IS NULL AND expires_at > now()``.
    Expiry is passive: a crashed holder never renews, the lease lapses, and a
    peer may then claim it. No background reaper is needed.
    """

    __tablename__ = "leases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session: Mapped[str] = mapped_column(Text, nullable=False)
    device: Mapped[str] = mapped_column(Text, nullable=False)
    holder: Mapped[str] = mapped_column(Text, nullable=False)  # token name (see auth.identify)
    cwd: Mapped[str | None] = mapped_column(Text)              # project dir (for revive)
    repo: Mapped[str | None] = mapped_column(Text)             # git repo name (topic-overlap match)
    branch: Mapped[str | None] = mapped_column(Text)           # git branch (finer overlap signal)
    title: Mapped[str | None] = mapped_column(Text)            # CC ai-title
    recap: Mapped[str | None] = mapped_column(Text)            # compact-summary head / last prompt
    model: Mapped[str | None] = mapped_column(Text)            # model id from last assistant msg
    #: What the holder is doing right now: working | waiting | input. Reported by
    #: the lifecycle hook, never inferred here.
    #:
    #: ``state_at`` is not decoration and not ``updated_at``. A state is only as
    #: good as its age — "working" said twenty minutes ago describes a pane that
    #: looks busy and has not moved — so the pair travels together and every
    #: consumer decides staleness for itself. It cannot be recovered from the
    #: lease's own timestamps: ``acquired_at`` is fixed at first claim and
    #: ``expires_at`` moves on every heartbeat whether or not the state changed.
    #:
    #: ``stalled`` is deliberately NOT one of the values. It is what a reader
    #: concludes from a state and its age, and a board that stored it would be
    #: asserting something about a holder that stopped talking to it.
    state: Mapped[str | None] = mapped_column(Text)
    state_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: HOW FAR ALONG the work is: ``F0``, ``R1``, ``R1F``, ``R2`` … Reported by
    #: ``qb-stage`` at the moment it changes, because ``qb-stage`` is the only
    #: thing in the system that is *told* the stage. It cannot be derived from
    #: anything the board holds — a round number is handed to ``panel.py`` as
    #: ``--round <r>``, never worked out — so it is not in the repo, the process
    #: table or the posts log. It has to be said (#262).
    #:
    #: ``state`` and ``stage`` are read together and are not the same question:
    #: ``state`` says whether the pane is moving, ``stage`` says where it has got
    #: to. ``repo``, ``branch`` and ``title`` — every other field a fleet view
    #: shows — read identically at every stage of a PR's life.
    #:
    #: **Shape-checked, vocabulary not enforced** (1-6 alphanumerics, see
    #: :data:`app.api.leases.STAGE_RE`), which is exactly the bargain ``qb-stage``
    #: itself strikes. A skill inventing ``R4F`` must not need a server edit, and
    #: the two failure modes are lopsided: an unknown-but-well-formed token
    #: renders as six harmless characters, a rejected one stops a workflow to
    #: argue about a cosmetic field. No CHECK constraint, for the reason 0023
    #: gives about ``state``.
    #:
    #: **No ``stage_at``**, and that is decided rather than omitted.
    #: ``state_at`` earns its column because a ``working`` said twenty minutes
    #: ago describes a pane that looks busy and has not moved — staleness is the
    #: whole reading. A stage is much longer-lived: it changes a handful of times
    #: a day, and ``expires_at`` already bounds how stale an *active* lease's
    #: answer can be. A second timestamp would cost a column to sharpen a
    #: judgement nobody makes.
    #:
    #: NULL means **nobody said** — not "no stage" and certainly not "finished".
    #: It is the overwhelming majority case, and every renderer spells it out.
    stage: Mapped[str | None] = mapped_column(Text)
    #: WHY this session stopped, when something reported it: ``finished``,
    #: ``killed``, ``timed_out``, ``context_reset``, ``superseded``. The
    #: vocabulary lives at the edge (:data:`app.api.leases.END_REASONS`), not in
    #: a CHECK constraint, for the reason ``state`` gives above.
    #:
    #: NULL is not "we don't know why" — it is a lease nothing ever ended. That
    #: is the distinction the column exists for. ``released_at`` set with no
    #: reason is a handoff or a plain release; ``released_at`` NULL with
    #: ``expires_at`` in the past is a lease that merely LAPSED, which says
    #: nobody renewed and says nothing about whether the work finished or the
    #: agent died. Before this column those three read identically off the board,
    #: which is why a finished session and a slow one looked alike.
    end_reason: Mapped[str | None] = mapped_column(Text)
    ttl_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_leases_session", "session"),
        Index("ix_leases_repo", "repo"),
    )
