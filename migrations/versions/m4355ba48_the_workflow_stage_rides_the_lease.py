"""How far along the work is — the one fact a fleet view could not show (#262)

A fleet view answers who is live, on what repo, on what branch, doing what. None
of those four change between writing the first cut and coming out of the third
review round: `repo`, `branch` and `title` read identically at every stage of a
PR's life. The question they do not answer — is this one still writing the fix,
or is it three rounds deep? — already had an answer on the machine, in the pane
footer `statusline.sh` draws from `qb-stage`'s marker file. Cross-machine that
marker is not there to read, and same-machine nothing read it.

`stage` is that answer, on the lease: `F0`, `R1`, `R1F`, `R2` … reported by
`qb-stage` at the moment it changes, because `qb-stage` is the only thing in the
system that is *told* it. A round number is handed to `panel.py` as
`--round <r>` and never derived, so it cannot be recovered from the repo, the
process table or the posts log. `model` is the precedent: a per-session fact the
holder reports because nobody else can know it.

Text and not an enum, and no CHECK constraint, for the reason 0023 gives about
`state`: the vocabulary is enforced at the edge (`app.api.leases.STAGE_RE`
checks the SHAPE — 1-6 alphanumerics — and deliberately not the words), so a
skill adding `R4F` costs a literal at worst and never a migration. The two
failure modes are lopsided: an unknown-but-well-formed token renders as six
harmless characters on a status bar, where a rejected one stops a workflow to
argue about a cosmetic field.

Nullable, no default, no backfill. NULL means **nobody said**, which is the true
thing about every lease on the board today and about most leases afterwards —
most sessions never call `qb-stage` at all. A default would invent a stage for
every live lease at deploy time, and inventing `F0` for a session that never
reported one is the confident-wrong answer the field exists to remove. Every
renderer spells the NULL out rather than leaving a blank cell that reads as a
stage.

No `stage_at` beside it, and that is decided rather than omitted. `state_at`
earns its column because `working` said twenty minutes ago describes a pane that
looks busy and has not moved. A stage changes a handful of times a day and the
lease's own `expires_at` already bounds how stale an active one can be, so a
second timestamp would cost a column to sharpen a judgement nobody makes.

Revision ID: m4355ba48
Revises: med8bb81b
Create Date: 2026-08-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m4355ba48"
down_revision: str | None = "med8bb81b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leases", sa.Column("stage", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("leases", "stage")
