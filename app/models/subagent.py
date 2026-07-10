from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Subagent(Base):
    """A live sub-agent (the Task/Agent tool) running inside a parent session.

    Sub-agents run inside the parent Claude Code process and fire no lifecycle
    hooks of their own, so leases/presence never see them. A Task-tool
    ``PreToolUse`` hook registers one here on spawn and a ``PostToolUse`` hook
    ends it; a TTL lets a crashed fan-out lapse without a reaper (mirroring the
    lease model). This is *current-state only* and never writes to the posts
    log, so sub-agent churn adds zero board noise.

    Active = ``ended_at IS NULL AND expires_at > now()``.
    """

    __tablename__ = "subagents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_session: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)  # unique within parent_session
    label: Mapped[str | None] = mapped_column(Text)  # e.g. "Explore: board frontend"
    cwd: Mapped[str | None] = mapped_column(Text)
    device: Mapped[str | None] = mapped_column(Text)
    holder: Mapped[str] = mapped_column(Text, nullable=False)  # token name of the parent
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("parent_session", "agent_id", name="uq_subagent_parent_agent"),
        Index("ix_subagents_parent_session", "parent_session"),
        Index("ix_subagents_cwd", "cwd"),
    )
