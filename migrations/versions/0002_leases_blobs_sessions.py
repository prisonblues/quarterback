"""v2: blobs, sessions, leases

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-05

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blobs",
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("sha"),
    )
    op.create_table(
        "sessions",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("latest_blob", sa.Text(), nullable=True),
        sa.Column("device", sa.Text(), nullable=True),
        sa.Column("holder", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("session"),
    )
    op.create_table(
        "leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("device", sa.Text(), nullable=False),
        sa.Column("holder", sa.Text(), nullable=False),
        sa.Column("ttl_seconds", sa.BigInteger(), nullable=False),
        sa.Column(
            "acquired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_leases_session", "leases", ["session"])


def downgrade() -> None:
    op.drop_index("ix_leases_session", table_name="leases")
    op.drop_table("leases")
    op.drop_table("sessions")
    op.drop_table("blobs")
