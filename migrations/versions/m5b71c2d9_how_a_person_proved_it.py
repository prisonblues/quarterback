"""how a person proved it, beside who they were (#477, #479)

`human()` has two methods now — an edge-vouched browser, and a person's own key
presented to the agent host — and they produce the SAME identity on purpose: a
person is one author however they arrived.

They are not the same event, though. The key sits on a workstation readable by
the processes running there, so an agent that goes looking can find it and author
as a person; that is accepted deliberately and written down in #479. A row that
recorded only `set_by` could not tell an afternoon's browser write from a
dashboard's afterwards, which is precisely when somebody asks.

Additive and NULL on arrival. Null means "not recorded" rather than "some other
method": every row written after this exists has an answer, and the rows written
before it honestly do not. No back-fill — a guess in this column would be the one
value a reader must be able to distrust, sitting in the column they consult to
decide whether to trust the row.

Revision ID: m5b71c2d9
Revises: m3a9c41e7
Create Date: 2026-08-26 09:40:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'm5b71c2d9'
# RE-POINTED after #480 landed. Both branches forked from mfe8671ba, so both
# named it as their parent and the chain grew two heads the moment they met —
# which is exactly what CI's "one migration head after the merge" check exists to
# catch, and it caught it. The columns are independent (a rank source on
# plan_items, a method on dial_settings), so the order between them carries no
# meaning; what matters is that there is an order at all.
down_revision: str | None = 'm3a9c41e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('dial_settings', sa.Column('set_via', sa.Text(), nullable=True))
    op.add_column('dial_settings', sa.Column('cleared_via', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('dial_settings', 'cleared_via')
    op.drop_column('dial_settings', 'set_via')
