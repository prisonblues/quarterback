from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: How a proposal was arrived at. The COLUMN is free text — for the reason
#: ``PlanItem.ref_kind``'s is, and it is the same trade: a second source should
#: not cost a migration, and the API still pins it to a ``Literal`` so adding one
#: is a code change that has to say what it means. Today there is exactly one,
#: and that is the point of this table: #232's planner cannot be told what its
#: last orders cost until something has been writing orders down, and the rules
#: can start writing them down without an agent existing at all.
SOURCES = ("deterministic",)


class OrderProposal(Base):
    """One recorded answer to "what order would the rules put this in?" (#232).

    This is the **prediction side of a ledger**. #232's argument is that a planner
    told only its own recent choices produces consistency rather than accuracy —
    "it will defend its prior order" — so what it must be given is a triple:
    *order proposed → what actually happened → the delta*. The two later terms
    need something to attach to, and until this table existed there was nothing:
    every ordering opinion the fleet ever formed was spoken in a session and lost
    with it, and #227's ask that a proposal record its *"expected rework avoided"*
    had nowhere to put the prediction and nothing to check it against.

    So a row here is a claim with a date on it, and the outcome side is
    deliberately absent rather than stubbed: an empty ``outcome`` column would
    invite the same "was this right?" question to be answered by whoever happened
    to be looking, which is exactly the self-grading loop #40 and #77 refuse.

    **It is not a second store of the order.** ``active_order`` is a snapshot of
    ``plan_items.rank`` at the moment of the proposal, kept because a suggestion
    means nothing without the sequence it was a suggestion against — and
    ``suggested_order`` is advisory: nothing in the board reads it back, and only
    a human, through ``POST /plan/reorder``, can put it into force. That is
    #232's non-privileged-writer rule, and it is why this table can exist before
    #183 is settled: it writes no order anybody has to obey.

    **Nothing derived is stored.** ``moves`` and ``changed`` regenerate from the
    two orders on read, because a stored copy is free to disagree with the source
    it came from — the failure the issue names when it says a planner must never
    regenerate from its own prior output.
    """

    __tablename__ = "plan_order_proposals"

    #: BIGSERIAL, like ``posts.id``, and for the same reason: "the last N
    #: proposals" is the read this table exists to serve, and a monotonic integer
    #: is the only ordering key that stays total when two proposals land in the
    #: same millisecond.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: The scope, EXACTLY as ``POST /plan/reorder`` means it: NULL is the
    #: fleet-wide list, not everything. A proposal is only useful if it can be
    #: applied, and the endpoint that applies it takes one scope at a time.
    repo: Mapped[str | None] = mapped_column(Text)
    #: The board identity that asked for the proposal to be recorded, and the
    #: session it came from. Not "the author of the order" — the rules wrote that,
    #: and ``rules_version`` is what identifies them.
    proposed_by: Mapped[str] = mapped_column(Text, nullable=False)
    session: Mapped[str | None] = mapped_column(Text)
    #: One of :data:`SOURCES`.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    #: Which rules produced it. Without this the ledger cannot tell a rule change
    #: from a world change, which is the only comparison it is for.
    rules_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: sha256 over every input the rules read and where it came from, excluding
    #: only the clock (``app.ordering._digest``). Two consecutive proposals with the
    #: same digest are the same proposal, which is what keeps a cron floor from
    #: filling this table with identical rows — while a panel run that arrived, or
    #: an outcome somebody recorded, does write a row even when the order is
    #: unchanged, because "the evidence moved and the answer did not" is one of the
    #: things this table exists to be able to say.
    inputs_digest: Mapped[str] = mapped_column(Text, nullable=False)
    #: Whether changed-file overlap was available when this was computed. Its
    #: query is #101 and still open, so early rows will say ``false`` — and a row
    #: that cannot say would be a row nobody can interpret later.
    overlap_known: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: The sequence in force when the proposal was made, and the one proposed.
    #: Lists of plan-item ids, in ``POST /plan/reorder``'s ``order`` shape.
    active_order: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    suggested_order: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    #: Per item: where it landed, on what basis, the rules that put it there, the
    #: facts they read and — in ``evidence`` — which panel run those facts came
    #: from. The half that makes the row a record rather than an assertion: #227
    #: asks a proposal to name the exact inputs used, and a stored reading that
    #: cannot be traced to its source cannot be checked afterwards.
    placements: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    #: Groups of items no rule separated — the remainder a model would be asked
    #: about — and any dependency cycle the rules could not resolve.
    ambiguous: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    cycles: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    #: What the gather could not read, named rather than omitted: an item with no
    #: PR ref, a PR the board has never panelled, an unresolvable ref, and overlap
    #: itself while #101 is open. An order that silently treats "no evidence" as
    #: "no problem" is the one this field exists to prevent being read that way.
    unknown: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        # The read this table serves: the last N for a scope. ``id`` descending
        # rather than ``ts``, so a page is stable when two rows share a timestamp.
        Index("ix_plan_order_proposals_repo_id", "repo", "id"),
    )
