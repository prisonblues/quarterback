"""Why a session stopped — the field that tells a finished session from a slow one.

Until now the board had exactly two facts about the end of a session, and neither
of them is a report. ``released_at`` says somebody let go, without saying whether
the work finished, the pane was closed or the context was reset. ``expires_at`` in
the past with ``released_at`` still NULL says only that nobody renewed — a crashed
holder and an agent thinking for thirty-one minutes produce the identical row
(#252). So the one question a fleet dashboard is for — is that seat done, or is it
slow? — had no column behind it.

``end_reason`` is that column: ``finished``, ``killed``, ``timed_out``,
``context_reset`` or ``superseded``, written by whatever actually observed the
ending. Set means *reported*; NULL means *nobody said*, which is the true thing
about every lease that ever lapsed and about every one on the board today.

No CHECK constraint and no enum type, for the reason 0023 gives about ``state``:
the vocabulary is enforced at the edge (``app.api.leases.END_REASONS``), so a sixth
reason costs a literal rather than a migration, and an unknown one is a 422 instead
of a row.

Nullable, no default, no backfill. A default would invent a reason for every live
lease at deploy time, and inventing ``finished`` for a session nobody ended is
precisely the confident-wrong answer this field exists to remove.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leases", sa.Column("end_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("leases", "end_reason")
