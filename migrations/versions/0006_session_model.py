"""v2.4: session model

Adds ``model`` (the model id from the transcript's last assistant message) to
``leases`` and ``sessions`` so the board can show what each session is running on.
Nullable.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("leases", "sessions"):
        op.add_column(table, sa.Column("model", sa.Text(), nullable=True))


def downgrade() -> None:
    for table in ("leases", "sessions"):
        op.drop_column(table, "model")
