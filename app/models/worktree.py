from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Worktree(Base):
    """One registered git worktree on one device — the cross-worktree discovery index.

    The server can't see the repos (they live on the devices), so each device
    reports a snapshot via PUT /worktrees. ``commits`` is a small recent slice
    ([{sha, subject}]) so the board can answer "which worktree has commit X".
    """

    __tablename__ = "worktrees"

    device: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    repo: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    head_sha: Mapped[str | None] = mapped_column(Text)
    commits: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_worktrees_repo", "repo"),
        Index("ix_worktrees_head", "head_sha"),
    )
