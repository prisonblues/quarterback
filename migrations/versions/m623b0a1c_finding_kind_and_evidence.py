"""A review finding records what the judge typed it as (#623)

``kind`` is one of ``regression``, ``claim_miss`` or ``observation``, or NULL for
every finding recorded before the judge typed anything, which keeps blocking by
verdict as it always did. ``evidence`` is the demonstration a regression or
claim_miss was typed on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m623b0a1c"
down_revision: str | None = "mfc132217"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_findings", sa.Column("kind", sa.Text(), nullable=True))
    op.add_column("review_findings", sa.Column("evidence", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_findings", "evidence")
    op.drop_column("review_findings", "kind")
