from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Text, func
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
    #: ``owner/name`` lower-cased — the origin slug, folded on the write through
    #: :func:`app.claimkey.canonical_repo` (#350). NULL where the device's
    #: checkout has no GitHub-style remote to read one off.
    repo: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(Text)
    head_sha: Mapped[str | None] = mapped_column(Text)
    commits: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # Sync state (v2.8): what the device knows about its own tracking branch, so
    # /sync can answer "is this checkout stale" without the server running git.
    upstream: Mapped[str | None] = mapped_column(Text)      # e.g. "origin/main"
    remote_sha: Mapped[str | None] = mapped_column(Text)    # upstream ref at report time
    ahead: Mapped[int | None] = mapped_column(Integer)      # local commits not on upstream
    behind: Mapped[int | None] = mapped_column(Integer)     # upstream commits not local
    dirty: Mapped[bool | None] = mapped_column(Boolean)     # uncommitted tracked changes
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # One repository, one stored spelling (#350, migration 0034). `PUT
        # /worktrees` folds through `canonical_repo`, and this is what lets
        # `GET /worktrees?repo=` compare the column rather than fold it — which
        # is what keeps `ix_worktrees_repo` serving the query.
        #
        # Case and surrounding whitespace only, NOT `owner/name` shape: the shape
        # is refused at ingest where a caller can be told why, and a constraint
        # asserting it would make 0034 abort on a legacy row rather than make it
        # canonical. `btrim` is given its character class because the
        # one-argument form trims ordinary spaces alone; vertical tab is `\013`
        # and never `\v`, since Postgres has no `\v` escape and the class would
        # gain the LETTER `v` — `btrim('vercel/next', …)` is `'ercel/next'`.
        CheckConstraint(r"repo IS NULL OR repo = lower(btrim(repo, E' \t\n\r\f\013'))",
                        name="ck_worktrees_repo_canonical"),
        Index("ix_worktrees_repo", "repo"),
        Index("ix_worktrees_head", "head_sha"),
    )
