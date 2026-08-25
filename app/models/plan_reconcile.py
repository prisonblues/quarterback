from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: What a pass can find. The names are `qb-reconcile`'s own, unchanged, because two
#: vocabularies for one observation is the drift this table exists to end — and
#: because the tool's five conditions are the considered list (#255), not an
#: enumeration to re-litigate here.
CONDITIONS = (
    "done_candidate",       # the item outlived its work: closed as completed, or merged
    "dropped_candidate",    # the work was abandoned: closed unmerged / not planned
    "stale_claim",          # claimed, but the claim does not describe the present
    "note_contradicted",    # the note asserts a readiness the review record denies
    "untracked_pr",         # an open PR no open plan item accounts for
)


class PlanReconcile(Base):
    """What the last reconcile pass found about one plan ref (#463).

    **The board cannot compute this and must not try.** It has no forge — that is
    #327's standing decision and the reason `qb-reconcile` lives on a host with
    `gh` rather than in here. So the observation arrives from a client, and this
    is where it lands: one row per ref a pass had something to say about, replaced
    wholesale each time that repo is reported.

    **It is a record, not a state.** Nothing here changes a plan item; `state`
    stays whatever a human or an agent set it to, and `next` still returns a
    flagged item rather than skipping it. The point is narrower and was the whole
    complaint: the plan and the pass were two facts on one board that never met,
    so `plan_read` answered `next: #449` at 10:40Z while a finding seven minutes
    old said #449 had been closed at 07:33Z. Now the read carries what the pass
    saw, and the caveat says it.

    **Why not the posts table, where this already half-lives.** The pass does post
    (`type: finding`, with `refs` naming each flagged issue), and a reader can see
    *that* an item was flagged — but not *which* condition, because :class:`Ref`
    is a generic dev-context link with four fields and `model_dump` drops
    anything else. A `done_candidate` and a `dropped_candidate` arrive identical,
    and those two are the ones that must never be confused: one is a record that
    has been overtaken, the other is a decision somebody has to make. The
    alternative was parsing the pass's rendered prose out of `detail`, which is
    the re-derivation this whole issue is about.

    **Why not a column on `plan_items`.** Two reasons, and the second is the one
    that decided it. An annotation from a tool does not belong in the row a person
    curates. And `plan_items.updated_at` is what `idle_days` and `stale` are
    computed from, so a pass touching every flagged item every fifteen minutes
    would keep resetting the staleness clock — a plan whose rows can never look
    idle because something keeps writing to them is a worse instrument than one
    that is merely out of date.
    """

    __tablename__ = "plan_reconcile"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The scope the ref belongs to — `owner/name`, matching `plan_items.repo` so
    #: the join is on the value the plan already keys by.
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    #: `issue` or `pr`, and the pair is how a ref is addressed everywhere else on
    #: this board: a PR and an issue can share a number (`app.claimkey`), so the
    #: number alone identifies nothing.
    ref_kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref_value: Mapped[str] = mapped_column(Text, nullable=False)
    #: One of :data:`CONDITIONS`. Not a database enum: the list is a client's and
    #: will grow there first, and a CHECK that has to be migrated in lockstep with
    #: a tool on another host is how a pass starts being rejected wholesale for
    #: reporting something new.
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    #: The pass's own sentence about this ref — "open item, but issue#449 is
    #: closed as completed". Stored rather than re-derived so the caveat quotes
    #: the thing that was actually observed, and so a condition this board does
    #: not yet know still reads as something.
    said: Mapped[str | None] = mapped_column(Text)
    #: WHEN IT WAS FIRST SEEN, preserved across passes, and the reason this is a
    #: table of refs rather than a blob per pass. "#156 has been a done candidate
    #: since 2026-08-23" is the datum that turns a report into an argument; the
    #: pass itself cannot say it, since it holds no history.
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: The newest pass that still said it. Equal to `first_seen` on the first
    #: sighting; a row absent from a later pass for its repo is deleted rather
    #: than left to age, because "the pass no longer says this" is the whole of
    #: what resolution means here.
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: The machine whose pass reported it. Two hosts run the timer today and post
    #: the same findings minutes apart, so this says which one wrote the row that
    #: is here — not a claim that the other disagreed.
    reported_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # One row per ref per scope: a second pass updates rather than accumulates,
        # which is what makes "replace this repo's set" an idempotent write and two
        # hosts reporting the same pass harmless.
        UniqueConstraint("repo", "ref_kind", "ref_value", name="uq_plan_reconcile_ref"),
        # The read's shape: `plan_read` asks for one scope's rows, or the fleet's.
        Index("ix_plan_reconcile_repo", "repo"),
    )
