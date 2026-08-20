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
