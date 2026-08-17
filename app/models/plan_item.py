from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: The states an item can be in. ``dropped`` is not ``done``: one says the work
#: happened, the other says a human decided it should not. Collapsing them would
#: make the plan's own history unreadable a fortnight later.
#:
#: The check constraint below is built from this tuple rather than restating it,
#: so the declared vocabulary and the enforced one cannot drift. (The API's
#: ``Literal``s are deliberately narrower: an agent may not spell ``done`` on the
#: update endpoint, because deciding an item is finished is a different verb.)
STATES = ("open", "done", "dropped")

_STATE_LIST = ", ".join(f"'{s}'" for s in STATES)


class PlanItem(Base):
    """One line of "what is next", ordered, and pointing at an issue.

    The board knew who was here and what they had just published; it did not know
    what to do next. That lived in an untracked ``plan.md`` on one machine, in a
    human's head, and in 26 unordered issues — so an agent starting cold guessed,
    and three of them once fixed the same red CI job in one morning.

    **It never restates an issue.** ``title`` is a handle and ``ref_kind`` /
    ``ref_value`` are the link; the *what* and the *why* stay in GitHub, and
    ``ix_plan_items_open_ref`` makes "one open item per issue" a database fact
    rather than a convention. What lives here is the half an issue cannot hold:
    the order, the reasoning behind it (``note``), the phase, and the
    dependencies.

    **There is no holder column, deliberately.** An item is claimed when a live
    :class:`~app.models.resource_lease.ResourceLease` exists for its claim key —
    ``kind='work'``, and for an issue-backed item the very key agents already
    take by hand (``prisonblues/quarterback#142``). So the claim is atomic at the
    unique index, expires passively with no reaper, and a claim taken through the
    plain ``POST /claim`` shows up in the plan without the claimant doing
    anything. Two implementations of "who has this right now" is what #99 was
    filed to avoid.
    """

    __tablename__ = "plan_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: NULL is fleet scope — the plan spans repos, as does the fleet.
    repo: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``issue`` or ``pr``. The COLUMN is free text for the same reason
    #: ``ResourceLease.kind`` is — a third kind should not cost a migration — but
    #: the API pins it to a ``Literal``, so adding one is still a code change:
    #: every kind needs a URL template to render and a claim key that cannot
    #: collide with another kind's. Permissive storage, decided intake.
    ref_kind: Mapped[str | None] = mapped_column(Text)
    ref_value: Mapped[str | None] = mapped_column(Text)
    phase: Mapped[str | None] = mapped_column(Text)
    #: Position in the list. Rewritten wholesale by a reorder; see the migration
    #: on why this is an integer and not a fraction.
    rank: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Ids of the items this one waits on, as strings. "not yet" as a fact
    #: instead of a convention.
    depends_on: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    state: Mapped[str] = mapped_column(Text, server_default=text("'open'"), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Last touched. Surfaced on every read so a phase nobody has moved in a
    #: fortnight is visibly stale — a plan that is believed and wrong is worse
    #: than no plan.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(f"state IN ({_STATE_LIST})", name="ck_plan_items_state"),
        CheckConstraint("(ref_kind IS NULL) = (ref_value IS NULL)", name="ck_plan_items_ref_pair"),
        Index("ix_plan_items_repo_rank", "repo", "rank"),
        Index("ix_plan_items_open_ref", text("COALESCE(repo, '')"), "ref_kind", "ref_value",
              unique=True,
              postgresql_where=text("ref_value IS NOT NULL AND state = 'open'")),
    )
