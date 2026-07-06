"""v2.3: session title + recap

Adds ``title`` (CC's generated ``ai-title``) and ``recap`` (the ``isCompactSummary``
head, else the last prompt) to ``leases`` and ``sessions`` — so the board can name
and summarise each session instead of showing identical "started on <repo>" rows.
Both nullable.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("leases", "sessions"):
        op.add_column(table, sa.Column("title", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("recap", sa.Text(), nullable=True))


def downgrade() -> None:
    for table in ("leases", "sessions"):
        op.drop_column(table, "recap")
        op.drop_column(table, "title")
