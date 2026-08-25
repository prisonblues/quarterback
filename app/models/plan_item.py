from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
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

#: How this item's rank came to be — WHO decided it sits there, which is the one
#: thing 28 ranked rows could not say (#183). The plan was seeded, an agent was
#: told mid-seed that #85 was near-top priority, and the only place it could
#: record that was prose: a ``phase`` reading ``"TOP PRIORITY — Rich, 23:00"`` and
#: a ``note`` opening ``RANK IS WRONG AND A HUMAN MUST FIX IT``. Meanwhile ranks
#: 1-17 were a real order and 18-28 were the sequence the adds happened to arrive
#: in, with nothing in the data telling the two apart.
#:
#: * ``appended`` — nobody chose it. It went last because that is all
#:   ``POST /plan/item`` could do, which is an ordering judgement ("this is the
#:   least important open item") asserted on the caller's behalf without being
#:   asked.
#: * ``submitted`` — it arrived inside a ``POST /plan/submit`` batch, below an
#:   item the submitter deliberately put above it. The batch's FIRST item is
#:   ``appended``, not ``submitted``: where the block itself sits was decided by
#:   "after everything already here" and by nobody, so each submission leaves
#:   exactly one position nobody chose.
#: * ``placed`` — an agent said where it goes, relative to a named neighbour.
#: * ``ordered`` — a human set it with ``POST /plan/reorder``.
#: * ``picked-up`` — nobody chose it either, and unlike ``appended`` that is not
#:   a gap. ``POST /claim`` wrote the row because an agent took the work, and it
#:   sits at the top because it is IN FLIGHT — a statement of fact about now, not
#:   a judgement about merit (#427). It costs the human's order nothing, because
#:   a claimed item is skipped by ``next``: the first free pick is unchanged.
#:
#: Only ``ordered`` is the plan's shared intent. The rest are how the row got
#: there, and ``GET /plan`` says so rather than leaving a reader to infer it from
#: a note somebody remembered to write.
#:
#: ``appended`` is the only one that counts as UNCHOSEN in ``order_trust``. A
#: ``picked-up`` row is at a position somebody's action decided even though no
#: one ranked it, so a plan full of in-flight work is still a plan you can
#: believe — see :func:`app.api.plan._order_trust`.
RANK_SOURCES = ("appended", "submitted", "placed", "ordered", "picked-up")

_RANK_SOURCE_LIST = ", ".join(f"'{s}'" for s in RANK_SOURCES)


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
    the order, the reasoning behind it (``note``), the plan it belongs to, and the
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
    #: The plan this item belongs to, or NULL for a loose one. Replaced the
    #: free-text ``phase`` (#172): a phase was a string two agents could
    #: spell two ways, owned by nothing, claimable by nobody and finishable never.
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), index=True)
    #: Position in the list. Rewritten wholesale by a reorder; see the migration
    #: on why this is an integer and not a fraction.
    rank: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: How that rank came to be — see :data:`RANK_SOURCES`. Defaulted in the
    #: database rather than in Python so a row written by a migration or by hand
    #: still says something true about itself.
    rank_source: Mapped[str] = mapped_column(
        Text, server_default=text("'appended'"), nullable=False)
    #: Whose stated priority a ``placed`` item transcribes — "Rich, 23:00". A
    #: field rather than a sentence in ``note``, because the workaround #183
    #: documents was an agent with a priority it had been told and no channel to
    #: record it in. Only ever set alongside a position: on its own it would be
    #: the free-text priority channel this replaced.
    placed_for: Mapped[str | None] = mapped_column(Text)
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
    #: Last touched. Surfaced on every read so a plan nobody has moved in a
    #: fortnight is visibly stale — a plan that is believed and wrong is worse
    #: than no plan. It is also half of the enclosing PLAN's staleness: work
    #: happens to items, so the freshest item in a plan is what says the plan is
    #: alive (see :class:`~app.models.plan.Plan.updated_at`).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(f"state IN ({_STATE_LIST})", name="ck_plan_items_state"),
        CheckConstraint(f"rank_source IN ({_RANK_SOURCE_LIST})",
                        name="ck_plan_items_rank_source"),
        CheckConstraint("(ref_kind IS NULL) = (ref_value IS NULL)", name="ck_plan_items_ref_pair"),
        Index("ix_plan_items_repo_rank", "repo", "rank"),
        Index("ix_plan_items_open_ref", text("COALESCE(repo, '')"), "ref_kind", "ref_value",
              unique=True,
              postgresql_where=text("ref_value IS NOT NULL AND state = 'open'")),
    )
