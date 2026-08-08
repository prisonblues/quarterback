"""v2.8: worktree sync state (upstream, ahead/behind, dirty)

Adds the tracking-branch facts a device already knows but used to throw away, so
``/sync`` can tell a peer "your checkout is behind" without the board server ever
running git. Nullable throughout — worktrees registered by an older MCP server
simply report no upstream state and fall back to the published-commit comparison.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("worktrees", sa.Column("upstream", sa.Text(), nullable=True))
    op.add_column("worktrees", sa.Column("remote_sha", sa.Text(), nullable=True))
    op.add_column("worktrees", sa.Column("ahead", sa.Integer(), nullable=True))
    op.add_column("worktrees", sa.Column("behind", sa.Integer(), nullable=True))
    op.add_column("worktrees", sa.Column("dirty", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("worktrees", "dirty")
    op.drop_column("worktrees", "behind")
    op.drop_column("worktrees", "ahead")
    op.drop_column("worktrees", "remote_sha")
    op.drop_column("worktrees", "upstream")
