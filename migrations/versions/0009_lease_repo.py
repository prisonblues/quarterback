"""v2.7: lease repo + branch (for topic-scoped self-discovery)

Adds ``repo`` and ``branch`` to ``leases`` so ``/active`` and ``/overlap`` can
match live agents by the repository (and branch) they're working in, not just an
identical ``cwd``. The hook already computes both from git; this persists them.
Nullable — older rows and non-git sessions are fine.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leases", sa.Column("repo", sa.Text(), nullable=True))
    op.add_column("leases", sa.Column("branch", sa.Text(), nullable=True))
    op.create_index("ix_leases_repo", "leases", ["repo"])


def downgrade() -> None:
    op.drop_index("ix_leases_repo", table_name="leases")
    op.drop_column("leases", "branch")
    op.drop_column("leases", "repo")
