"""What the holder of a lease is doing, and when it last said so.

A lease has always answered *who* is on a session and *where* — holder, cwd,
repo, branch, and the ai-title of what they are up to. It has never answered
whether they are moving. For one agent that gap is invisible; for a wall of
seats it is the whole question, because a pane that finished ten minutes ago,
a pane waiting on a permission prompt and a pane thinking hard all render
identically to anyone looking at them.

`state` is `working | waiting | input`, reported by the lifecycle hook that
already sends the lease. It is not inferred here and it is not derived from
traffic: "the agent finished its turn" is a fact only the hook is told, and any
attempt to guess it from lease renewals reads a slow turn as a finished one.

`state_at` is the half that makes the other half usable. A state is only as good
as its age — `working` last said twenty minutes ago describes a pane that looks
busy and has not moved, which is the failure mode this is for — and neither of
the timestamps already on the row can stand in for it: `acquired_at` is fixed at
first claim, and `expires_at` moves on every heartbeat whether or not the state
changed. So the pair is stored together and every reader decides staleness for
itself, at whatever threshold suits it.

There is deliberately no `stalled` value and no constraint enumerating the three
that exist. Stalled is a *conclusion* a reader draws from a state and its age,
never something a holder reports about itself; the vocabulary is enforced at the
edge (`LeaseIn.state` is a Literal, so an unknown value is a 422 rather than a
row), which keeps a schema migration out of the path of adding a fourth state.

Nullable, no default, no backfill: every live lease predates the field, and
`NULL` says exactly the true thing about them — this holder has not reported a
state. A default of `working` would have invented one for every session on the
board at deploy time.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leases", sa.Column("state", sa.Text(), nullable=True))
    op.add_column("leases", sa.Column("state_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("leases", "state_at")
    op.drop_column("leases", "state")
