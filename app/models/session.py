from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SessionRecord(Base):
    """Durable pointer to the latest JSONL blob for a Claude Code session.

    ``session`` is an opaque client key (the session uuid / cwd-encoded path);
    the server never interprets it. Leases are ephemeral claims; this row is the
    stable record of "the last blob a device handed off for this session", which
    a peer pulls after acquiring the lease.
    """

    __tablename__ = "sessions"

    session: Mapped[str] = mapped_column(Text, primary_key=True)
    latest_blob: Mapped[str | None] = mapped_column(Text)
    cwd: Mapped[str | None] = mapped_column(Text)      # project dir for `claude --resume`
    title: Mapped[str | None] = mapped_column(Text)    # CC ai-title
    recap: Mapped[str | None] = mapped_column(Text)    # compact-summary head / last prompt
    device: Mapped[str | None] = mapped_column(Text)
    holder: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
