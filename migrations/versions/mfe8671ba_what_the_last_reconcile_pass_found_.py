"""what the last reconcile pass found about a plan ref (#463)

One row per ref a `qb-reconcile` pass had something to say about. The board has no
forge and must not grow one (#327), so the observation arrives from the host that
does have `gh`; this is where it lands so that `plan_read` can carry it.

Additive and empty on arrival: nothing reads a row that is not there, and the
first pass after deploy fills it. No back-fill, because the pass runs every
fifteen minutes on two hosts and one of them will have written the present state
before anybody notices the table exists.

Revision ID: mfe8671ba
Revises: m1986deca
Create Date: 2026-08-25 14:22:26.885159

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'mfe8671ba'
down_revision: str | None = 'm1986deca'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('plan_reconcile',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('repo', sa.Text(), nullable=False),
    sa.Column('ref_kind', sa.Text(), nullable=False),
    sa.Column('ref_value', sa.Text(), nullable=False),
    sa.Column('condition', sa.Text(), nullable=False),
    sa.Column('said', sa.Text(), nullable=True),
    sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('reported_by', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('repo', 'ref_kind', 'ref_value', name='uq_plan_reconcile_ref')
    )
    op.create_index('ix_plan_reconcile_repo', 'plan_reconcile', ['repo'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_plan_reconcile_repo', table_name='plan_reconcile')
    op.drop_table('plan_reconcile')
