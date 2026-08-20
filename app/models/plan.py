from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: The states a plan can be in, and the same three an item has — a plan that was
#: abandoned is not a plan that was finished, and a fortnight later the difference
#: is the only thing that makes the history readable.
STATES = ("open", "done", "dropped")

_STATE_LIST = ", ".join(f"'{s}'" for s in STATES)


class Plan(Base):
    """A named, claimable unit of intent, holding an ordered list of items.

    **This replaces ``plan_items.phase``, and the reason is #172.** A phase was a
    free-text string on an item, owned by nothing and bounded by nothing: two
    agents writing "stage 1" and "Stage 1" made two phases, nobody could claim
    one, nothing could say a phase was finished, and a plan submitted item by item
    could be raided half-written by a second agent. That is the same
    two-spellings-of-one-thing defect the claim key had, in the plan's own table.

    A row fixes all four at once: the label is unique per scope while the plan is
    open, the plan has a state, and — because it is a row with an id — it can be
    claimed through the same :class:`~app.models.resource_lease.ResourceLease`
    every other claim goes through.

    **Why a plan is claimable at all.** #172's one genuinely fuzzy race is two
    agents surveying the same vague problem in parallel: there are no items yet,
    so there is nothing exact to claim. A plan-level claim is coarse enough to
    cover the survey and specific enough to be a real key
    (``work``/``plan:<uuid>``), and everything downstream of it is exact item
    keys. So this is the *only* place a coordination window is needed, and it
    converges into the structured path rather than staying a permanently softer
    one.

    **No holder column here either**, for the reason
    :class:`~app.models.plan_item.PlanItem` gives: a plan is claimed when a live
    resource lease exists for its derived key. Two implementations of "who has
    this right now" is the outcome #99 was filed to avoid, and this would have
    been the fourth feature to want one.
    """

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: NULL is fleet scope. A plan may span repos — #172 closes on that as an open
    #: question, and the answer this schema gives is the same one ``plan_items``
    #: already gave: a NULL scope is the fleet, and an item inside a fleet plan
    #: carries its own repo. So an epic that crosses quarterback and nix-fleet is
    #: expressible rather than being "a plan with a hole in it".
    repo: Mapped[str | None] = mapped_column(Text)
    #: The handle an agent says out loud. Unique per scope while open — see the
    #: index below.
    label: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why this plan, in the submitter's words. The half an issue cannot hold.
    note: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'open'"), nullable=False)
    added_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: When the PLAN ROW last changed — claimed, released, finished. Activity in
    #: the plan's ITEMS deliberately does not touch it, and the endpoint does not
    #: read this column alone because of that: ``stale`` is the later of this and
    #: the freshest item in the plan (``app.api.plan._plan_activity``). A plan is
    #: worked through its items — appending, claiming, finishing and moving them
    #: all left this timestamp alone — so a plan whose items were being worked
    #: daily reported ``stale: true`` after a fortnight, which is the opposite of
    #: what the flag is for. Bumping this row from each of those eight writes was
    #: the alternative, and it is eight places to forget plus a row lock on the
    #: plan for every item claim.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(f"state IN ({_STATE_LIST})", name="ck_plans_state"),
        CheckConstraint("length(btrim(label)) > 0", name="ck_plans_label"),
        # One open plan per label per scope, as a database fact rather than a
        # convention. Case-folded because "stage 1" and "Stage 1" are one plan
        # everywhere except a byte comparison — the same reasoning `_norm_scope`
        # already applies to a repo name, and the same defect one level up.
        Index("ix_plans_open_label", text("COALESCE(repo, '')"), text("lower(label)"),
              unique=True, postgresql_where=text("state = 'open'")),
        Index("ix_plans_repo_state", "repo", "state"),
    )
