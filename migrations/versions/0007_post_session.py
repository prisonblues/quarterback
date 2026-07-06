"""v2.5: link posts to their session

Adds ``posts.session`` (the Claude Code session_id the post came from) so the board
can be session-centric: a post is an event *within* a session, not a free-floating
row. Nullable — legacy posts and non-session callers stay null (a global bucket).
Refreshes the NOTIFY trigger to carry ``session`` on the live stream.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOTIFY = """
CREATE OR REPLACE FUNCTION quarterback_notify_post() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('quarterback_posts', json_build_object(
    'id', NEW.id,
    'ts', to_char(NEW.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'from', NEW.author,
    'session', NEW.session,
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

_NOTIFY_PREV = _NOTIFY.replace("    'session', NEW.session,\n", "")


def upgrade() -> None:
    op.add_column("posts", sa.Column("session", sa.Text(), nullable=True))
    op.create_index("ix_posts_session", "posts", ["session"])
    op.execute(_NOTIFY)


def downgrade() -> None:
    op.execute(_NOTIFY_PREV)
    op.drop_index("ix_posts_session", table_name="posts")
    op.drop_column("posts", "session")
