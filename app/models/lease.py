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
    ttl_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_leases_session", "session"),)
