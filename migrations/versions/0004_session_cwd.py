"""v2.2: session cwd (for one-click revive)

Adds ``cwd`` to ``leases`` and ``sessions`` so a peer can resume a session with
`claude --resume <id>` from the original project dir. Nullable — older rows and
clients that don't send it are fine.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leases", sa.Column("cwd", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("cwd", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "cwd")
    op.drop_column("leases", "cwd")
