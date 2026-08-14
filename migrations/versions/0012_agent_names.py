"""v2.11: board-designated agent names — (machine, key) -> name

Naming moves from the client to the board. A client sends a stable opaque key
and the board allocates a two-word name that is free on that machine; both
forms address the same agent, and only the name is written into history.

The partial unique index is the load-bearing part: it is what makes "pick a
name nothing live is using" safe when two agents start at the same instant.
Retired rows keep their name (history references it) but drop out of the index,
so the name is free for the next agent.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_names",
        sa.Column("machine", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "allocated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("machine", "key"),
    )
    op.create_index(
        "uq_agent_names_live",
        "agent_names",
        ["machine", "name"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index("ix_agent_names_machine_name", "agent_names", ["machine", "name"])


def downgrade() -> None:
    op.drop_index("ix_agent_names_machine_name", table_name="agent_names")
    op.drop_index("uq_agent_names_live", table_name="agent_names")
    op.drop_table("agent_names")
