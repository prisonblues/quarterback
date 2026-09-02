"""a pass that found nothing still ran (#695)

One row per scope saying that a `qb-reconcile` pass covered it, and when. The
findings table cannot carry this: a clean pass reports an empty set and
`report_reconcile` deletes the rows it did not re-report, so a healthy scope
leaves `plan_reconcile` empty and indistinguishable from one nothing has ever
reconciled. That ambiguity is what let `qb-doctor` ask a per-host question about a
fleet-singleton pass and call three hosts unwired for running the design.

Additive and empty on arrival. Nothing reads a row that is not there — the read
answers `null` and the monitor says so — and the first pass after deploy fills it,
which on a fifteen-minute timer is sooner than anybody will look. No back-fill is
possible even in principle: no table here records when a pass ran, which is the
whole of the bug.

Revision ID: mb41e07c9
Revises: md3a02ab5
Create Date: 2026-09-02 12:41:03.117204

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'mb41e07c9'
down_revision: str | None = 'md3a02ab5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'plan_reconcile_pass',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('repo', sa.Text(), nullable=False),
        sa.Column('last_pass_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('reported_by', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repo', name='uq_plan_reconcile_pass_repo'),
    )


def downgrade() -> None:
    op.drop_table('plan_reconcile_pass')
