"""v2.1: post refs + worktree registry

Adds ``posts.refs`` (dev-context links), refreshes the NOTIFY trigger to carry
them, and creates the ``worktrees`` registry for cross-worktree discovery.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-05

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# CREATE OR REPLACE so the live payload matches app.schemas.summary_tier, now
# including refs (COALESCE to [] to mirror the endpoint's `p.refs or []`).
NOTIFY_FN_V2 = """
CREATE OR REPLACE FUNCTION quarterback_notify_post() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('quarterback_posts', json_build_object(
    'id', NEW.id,
    'ts', to_char(NEW.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'from', NEW.author,
    'type', NEW.type,
    'summary', NEW.summary,
    're', NEW.re,
    'to', NEW.recipient,
    'detail_ref', NEW.detail_ref,
    'has_detail', (NEW.detail IS NOT NULL),
    'refs', COALESCE(NEW.refs, '[]'::jsonb)
  )::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

NOTIFY_FN_V1 = """
CREATE OR REPLACE FUNCTION quarterback_notify_post() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('quarterback_posts', json_build_object(
    'id', NEW.id,
    'ts', to_char(NEW.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'from', NEW.author,
    'type', NEW.type,
    'summary', NEW.summary,
    're', NEW.re,
    'to', NEW.recipient,
    'detail_ref', NEW.detail_ref,
    'has_detail', (NEW.detail IS NOT NULL)
  )::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column("posts", sa.Column("refs", postgresql.JSONB(), nullable=True))
    op.execute(NOTIFY_FN_V2)

    op.create_table(
        "worktrees",
        sa.Column("device", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("commits", postgresql.JSONB(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("device", "path"),
    )
    op.create_index("ix_worktrees_repo", "worktrees", ["repo"])
    op.create_index("ix_worktrees_head", "worktrees", ["head_sha"])


def downgrade() -> None:
    op.drop_index("ix_worktrees_head", table_name="worktrees")
    op.drop_index("ix_worktrees_repo", table_name="worktrees")
    op.drop_table("worktrees")
    op.execute(NOTIFY_FN_V1)
    op.drop_column("posts", "refs")
