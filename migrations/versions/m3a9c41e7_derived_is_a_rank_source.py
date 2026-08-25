"""An order an agent applied on a person's instruction is `derived`, not `ordered` (#478)

`POST /plan/reorder` accepts a delegated agent now, not only a person, so a rank
this endpoint writes is no longer necessarily a human's decision. Under the old
vocabulary it would have been written `ordered` anyway — the one word in the set
that means *a person chose this position* — and `GET /plan` would then report a
sequence an agent computed as one somebody typed.

That is exactly the substitution #183 exists to stop, one layer down. Its whole
argument is that "an order whose chosen and unchosen parts are indistinguishable
gets trusted uniformly, and usually too much", and a delegated reorder writing
`ordered` would make the two indistinguishable by construction — worse than the
appended rows #183 was filed about, because those at least *look* like an
accident.

So: a sixth value, for a rank that a rule and an instruction produced together.
`plan_order`'s deterministic five rules supply the sequence, a person supplies the
priorities and the word to go, and an agent applies it. Nobody involved is lying
about what happened, and the row can say so.

**`order_trust.trusted` deliberately does NOT go false on these.** `unchosen`
stays what it always was — the `appended` rows, the positions nobody chose at all
— and this migration's sibling (`m7c31f0d2`, `picked-up`) is the precedent and the
reason: counting a new source as untrusted "would have made the plan read as less
trustworthy for the sole reason that agents were working", swamping the signal
that the human's ordering has gaps with the signal that the fleet is busy. A
`derived` rank was asked for by a person and computed from facts; it is weaker
evidence than `ordered` and much stronger than `appended`, and the honest way to
say that is a count of its own beside the others rather than a boolean flipped.

Data-only in the other direction: nothing is rewritten and nothing backfilled.

Revision ID: m3a9c41e7
Revises: mfe8671ba
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import op

revision: str = "m3a9c41e7"
down_revision: str | None = "mfe8671ba"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_plan_items_rank_source"
_OLD = "rank_source IN ('appended', 'submitted', 'placed', 'ordered', 'picked-up')"
_NEW = ("rank_source IN ('appended', 'submitted', 'placed', 'ordered', "
        "'picked-up', 'derived')")


def upgrade() -> None:
    # Widening a CHECK: drop and recreate, because Postgres has no ALTER for the
    # expression. Nothing can violate the new one that did not violate the old,
    # and both statements are in this migration's transaction, so there is no
    # window in which a bad row could slip in.
    op.drop_constraint(_CONSTRAINT, "plan_items", type_="check")
    op.create_check_constraint(_CONSTRAINT, "plan_items", _NEW)


def downgrade() -> None:
    # **Lossy, and named rather than risked** — the same landing `m7c31f0d2` chose,
    # for the same reason. Rolling back narrows the vocabulary, so any row written
    # while this was deployed would fail the old constraint and take the whole
    # downgrade with it.
    #
    # `ordered` would be the tempting map here and it is the wrong one: it would
    # silently promote an agent-applied sequence to a human's decision, which is
    # the precise claim this migration exists to stop the board making — and a
    # downgrade is the worst moment to start making it, because nothing afterwards
    # records that it happened. `appended` understates instead: those rows count as
    # unchosen, so a rolled-back board reads its plan as LESS trusted than it did a
    # moment earlier. That is the correct direction to be wrong in. The rank itself
    # is untouched either way, so the list still reads in the same sequence.
    op.execute("UPDATE plan_items SET rank_source = 'appended' "
               "WHERE rank_source = 'derived'")
    op.drop_constraint(_CONSTRAINT, "plan_items", type_="check")
    op.create_check_constraint(_CONSTRAINT, "plan_items", _OLD)
