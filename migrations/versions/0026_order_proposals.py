"""vNEXT: recorded ordering proposals — the prediction side of #232's ledger

#232 asks for one agent that owns the order of the plan and is *told what its
last few orders actually cost*, "so it is the one autonomous agent here that can
be wrong in a way anybody notices". Build the agent first and there is nothing to
tell it: every ordering opinion this fleet has ever formed was spoken in a
session and lost with it. #227 asked a proposal to record its *"expected rework
avoided"* — a prediction — and noted that nothing ever checks it, because nothing
was ever written down to check.

This table is what a check can later be run against. It stores what the
deterministic rules (`app/ordering.py`) proposed, the sequence they proposed it
against, and the evidence for every placement — including which placements the
rules could **not** decide.

## Why the outcome columns are missing on purpose

The other half of #232's triple (*order proposed → what happened → the delta*) is
not here, and not stubbed either. A nullable `outcome` column would invite "was
this right?" to be answered by whoever is looking at the row, which is the
self-grading loop #40 and #77 both refuse; and the honest answer needs a merge
order, a rebase count and a staleness reading that this release does not gather.
An absent column is a visible gap. A null one reads like a question nobody
bothered to answer.

## Why it is not a board post

The board is the right store for "an agent said something" and this is that —
except that the read it has to serve is *"the last N proposals for this repo,
with their evidence, as structured rows"*, which over `posts.detail` means every
consumer parsing JSON out of a text column and no index on the scope. #229's
discipline is not "everything is a post"; it is "do not build a second store of
something the board already holds", and the board holds no orderings.

## Why nothing derived is stored

No `moves`, no `changed`, no `derived_count`. All three fall out of
`active_order` and `suggested_order` on read, and a stored copy is free to
disagree with the two columns it came from — the exact failure #232 names when it
says a planner must regenerate from source and never from its own prior output.

## Nullability

Everything except `repo` and `session` is NOT NULL, and the JSONB columns default
to nothing: a proposal with no placements is not a proposal with unknown
evidence, it is a bug in the writer, and a row that cannot say which rules made
it (`rules_version`) or what they read (`inputs_digest`) is a row no later
analysis can interpret. `repo` is nullable because NULL is the fleet-wide scope,
exactly as in `plan_items`.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_order_proposals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("rules_version", sa.Integer(), nullable=False),
        sa.Column("inputs_digest", sa.Text(), nullable=False),
        sa.Column("overlap_known", sa.Boolean(), nullable=False),
        sa.Column("active_order", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("suggested_order", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("placements", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ambiguous", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cycles", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unknown", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # The one read this table serves: the last N for a scope, newest first.
    op.create_index("ix_plan_order_proposals_repo_id", "plan_order_proposals",
                    ["repo", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_plan_order_proposals_repo_id", table_name="plan_order_proposals")
    op.drop_table("plan_order_proposals")
