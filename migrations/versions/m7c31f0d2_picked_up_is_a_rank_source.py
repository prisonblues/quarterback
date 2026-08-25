"""Picking work up is a position somebody chose — `picked-up` says which (#427)

`POST /claim` now writes the plan item for the work being claimed, at the top of
its scope. That row needs a `rank_source`, and neither of the two candidates was
true about it.

`placed` would have the row claim an agent named a neighbour and chose where it
went relative to that neighbour. Nobody did. The row is at the top because
somebody started the work, which is not a comparison against anything already in
the list — and `placed_for` would then have to carry "picked up by
hermes/seat-quarterback-1", a fact about WHO rather than about whose priority the
position transcribes, which is the free-text priority channel #183 removed.

`appended` would be worse, and not only untrue. `order_trust` counts `appended`
rows as the positions nobody chose, so every claim taken would have made the plan
read as less trustworthy — `trusted: false`, a `next` carrying a caveat about an
order partly nobody's — for the sole reason that agents were working. The signal
that the human's ordering has gaps in it would have been swamped by the signal
that the fleet is busy, and they are not the same signal.

So: a fifth value, counted as chosen. What chose it is the act of picking the
work up, which is a real decision made by a real agent at a real moment, and the
row says so instead of borrowing a word that means something else.

**It costs the human's order nothing**, which is the reason this is safe to do
automatically. `next` is "the first item that is open, unclaimed and unblocked",
and every `picked-up` row is claimed by construction — the claim is what created
it. So the rows land above the ordered list and `next` walks straight past them
to the same free item it would have found before. The plan gains a true statement
about what is in flight and loses no ability to say what to do.

Data-only in the other direction: no rows are rewritten, nothing is backfilled.
Every item that exists keeps the source it was written with. A downgrade has to
deal with rows this vocabulary allowed and the old one did not, and it maps them
to `appended` — see below.

Revision ID: m7c31f0d2
Revises: m4355ba48
Create Date: 2026-08-24

"""

from __future__ import annotations

from alembic import op

revision: str = "m7c31f0d2"
down_revision: str | None = "m4355ba48"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_plan_items_rank_source"
_OLD = "rank_source IN ('appended', 'submitted', 'placed', 'ordered')"
_NEW = "rank_source IN ('appended', 'submitted', 'placed', 'ordered', 'picked-up')"


def upgrade() -> None:
    # Widening a CHECK: drop and recreate, because Postgres has no ALTER for the
    # expression. Nothing can violate the new one that did not violate the old,
    # so there is no scan to worry about beyond the validation Postgres does
    # anyway, and no window in which a bad row could slip in — both statements
    # are in this migration's transaction.
    op.drop_constraint(_CONSTRAINT, "plan_items", type_="check")
    op.create_check_constraint(_CONSTRAINT, "plan_items", _NEW)


def downgrade() -> None:
    # **Lossy, and the loss is named rather than risked.** Rolling back narrows
    # the vocabulary, so any row written while this was deployed would fail the
    # old constraint and take the whole downgrade with it. Mapping them to
    # `appended` first is the honest landing: it is the value that means "nobody
    # chose this position", which is the closest thing the old vocabulary can say
    # about a row the old code has no concept of.
    #
    # The consequence is deliberate and worth stating: those rows then count as
    # unchosen in `order_trust`, so a rolled-back board reads its plan as less
    # trusted than it did a moment earlier. That is the correct direction to be
    # wrong in — it understates confidence in an order rather than overstating
    # it, and the rank itself is untouched either way, so the list still reads in
    # exactly the same sequence.
    op.execute(
        "UPDATE plan_items SET rank_source = 'appended' WHERE rank_source = 'picked-up'")
    op.drop_constraint(_CONSTRAINT, "plan_items", type_="check")
    op.create_check_constraint(_CONSTRAINT, "plan_items", _OLD)
