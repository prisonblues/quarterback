"""v2.6: sub-agent visibility + the collision index

Adds the ``subagents`` table — a current-state registry of sub-agents (the
Task/Agent tool) live inside a parent session. Populated by Task-tool
PreToolUse/PostToolUse hooks; it never touches the posts log, so sub-agent
churn adds no board noise. Powers ``GET /active`` (the collision index) and the
sub-agent chips on session cards.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subagents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_session", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("device", sa.Text(), nullable=True),
        sa.Column("holder", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("parent_session", "agent_id", name="uq_subagent_parent_agent"),
    )
    op.create_index("ix_subagents_parent_session", "subagents", ["parent_session"])
    op.create_index("ix_subagents_cwd", "subagents", ["cwd"])


def downgrade() -> None:
    op.drop_index("ix_subagents_cwd", table_name="subagents")
    op.drop_index("ix_subagents_parent_session", table_name="subagents")
    op.drop_table("subagents")
