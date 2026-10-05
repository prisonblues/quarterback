"""Give blob GC an independent retention clock (#819).

Existing rows start with a full grace period on deployment; created_at remains
immutable. PostgreSQL's constant default avoids rewriting the blob contents.
"""
import sqlalchemy as sa
from alembic import op

revision = "m9c82a7e1"
down_revision = "m25dac5c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("blobs", sa.Column("last_used_at", sa.DateTime(timezone=True),
                                    server_default=sa.func.now(), nullable=False))


def downgrade() -> None:
    op.drop_column("blobs", "last_used_at")
