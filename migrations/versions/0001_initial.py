"""initial: posts table + summary-tier NOTIFY trigger

Revision ID: 0001
Revises:
Create Date: 2026-07-05

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fires on every insert and emits a summary-tier JSON payload on the
# `quarterback_posts` channel. The key set matches app.schemas.summary_tier so
# the SSE live leg can forward the payload verbatim (no re-fetch). `detail` is
# deliberately excluded — only whether it exists — to keep payloads small and
# well under Postgres' 8 KB NOTIFY cap.
NOTIFY_FN = """
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

CREATE_TRIGGER = """
CREATE TRIGGER quarterback_notify_post_trigger
AFTER INSERT ON posts
FOR EACH ROW EXECUTE FUNCTION quarterback_notify_post();
"""


def upgrade() -> None:
    op.create_table(
        "posts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("detail_ref", sa.Text(), nullable=True),
        sa.Column("re", sa.BigInteger(), nullable=True),
        sa.Column("recipient", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_posts_type", "posts", ["type"])
    op.create_index("ix_posts_recipient", "posts", ["recipient"])
    op.execute(NOTIFY_FN)
    op.execute(CREATE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS quarterback_notify_post_trigger ON posts")
    op.execute("DROP FUNCTION IF EXISTS quarterback_notify_post()")
    op.drop_index("ix_posts_recipient", table_name="posts")
    op.drop_index("ix_posts_type", table_name="posts")
    op.drop_table("posts")
