"""The rungs and the floors a round stopped on (#732)

`panel_rounds.round_stop` returns a dict of **24** keys. `StopIn` — the model
`ReviewIn.round_stop` binds it with — declares **6**. The other eighteen went on
the floor, silently, on every round this board has ever recorded, because
`StopIn` is `populate_by_name=True` with no `extra=` and pydantic v2's default is
`extra="ignore"`.

That is `m6bc45ff1`'s `converged` and `m7fc78723`'s `outstanding` exactly, one
tier down. The check written to end the class — `tests/test_payload_key_drift.py`
(#643) — reads the **top level** of the payload and says so twice in its own
docstrings, so a key nested under `round_stop` was never in its scope. #717 is
the proof that the gap was live: `outstanding` had been sent since #42, was bound
by nothing, and was found by a human noticing a number was missing while the
drift check stood green throughout.

## What is stored, and what is not

Eighteen keys, each decided once. Twelve are now bound; six are on a written-out
list in that test file with a reason each, which is the only other way past it.

**`stop_rungs`** — the eight `escalate_on` rungs plus #489's premise-brake state,
verbatim, keyed by the panel's own names. #712's complaint is that every rung "is
a claim about a cycle's series, and no endpoint serves one", and #710 is a
calibration assembled by hand out of report text. Part of why no endpoint serves
them is that they never reached a row.

One column and not nine. Each block is a measurement beside its own verdict —
`over` (the number crossed a limit) kept deliberately apart from `fired` (this
rung is why the cycle stopped) — and which scalar out of each matters is exactly
what #710 has yet to answer. Lifting a column per rung now would be this board
deciding that question from a position of never having held the numbers. Stored
whole, #712's series is one `ORDER BY round` over a cycle, and the columns can be
lifted later out of data that exists.

Refused WHOLE when it will not serialise or is over its cap, on `review_panel`'s
and `fix_pass`' rule: a rung set short one rung does not read as a smaller
measurement, it reads as a round on which that rung did not fire.

**`cleared_floor`** — the floor the round was required to clear. `m7fc78723`
stores a disposal SPLIT at this floor and does not store the floor, so a reader
holding `{"fixable": 2, "below_floor": 11}` cannot say what `below_floor` means.
It is derivable in principle: `Dials.cleared_floor` is a function of three dials
in `review_panel`. It is not derivable in practice by anything that must not
re-read the policy — a board-side derivation is a second reading of the rules
that produced the verdict stored beside it, which is what `m6bc45ff1` refuses for
`converged`. One word from the producer is the producer's answer, not a second
reading.

**`new_below_trigger_floor`, `repeated_below_trigger_floor`** — how many findings
this round raised, and how many an earlier round had already raised, that fell
under the trigger floor and so bought no round. Counts, not the key lists, on
`outstanding_counts`' rule: `len()` of a published list cannot disagree with that
list, and the keys are already on the round's own findings. Two columns and not a
sum, because the pair is the signal — one says the floor is turning work away at
the door, the other says work already inside the cycle never gets done, and a
repo whose backlog is all in the second has a different problem from one where it
is all in the first.

## What deliberately gets no column

`trigger_floor` is `Dials.round_trigger_floor` unchanged, `max_rounds` is
`Dials.max_rounds`, and `review_panel` has held both verbatim since #643.
`escalated_outstanding`, `declined_outstanding` and `narrowed` are
`outstanding.escalated`, `.declined` and `.narrowed` under second names, off the
same locals, and `outstanding_counts` already stores their lengths. `round` is
sent at the top level as well and is already a column. Six copies not made; six
lines written down instead, in the file that fails when a nineteenth key appears.

## Nullable, no server default, no backfill

NULL means the panel did not say — every row in this table today, every producer
too old to nest the key, and every rung set refused whole. **Never zero**, and
the two integer columns are the ones where that matters: "no new finding fell
below the floor" and "this producer does not measure it" are opposite readings
of the same round, and only one of them argues for lowering the floor. There is
no second copy to backfill from; a round payload lives in a temp directory on
whichever host ran the panel.

`stop_rungs` is deferred on the model for `rules`' and `fix_pass`' reason — nine
objects no list query needs. This migration does not express that: deferral is a
mapping property, not a schema one.

## Downgrade

`downgrade()` drops all four columns and **the data is gone**, on `m7fc78723`'s
terms and for its reason. Acceptable for columns no existing read depends on, and
written down here rather than discovered.

Not indexed, deliberately, on `converged`'s argument: every query on this table
is already selective on `(repo, pr)` or `ts`.

Revision ID: mb79c396d
Revises: m7fc78723
Create Date: 2026-09-03 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "mb79c396d"
down_revision: str | None = "m7fc78723"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("cleared_floor", sa.Text(), nullable=True))
    op.add_column(
        "review_runs",
        sa.Column("new_below_trigger_floor", sa.Integer(), nullable=True),
    )
    op.add_column(
        "review_runs",
        sa.Column("repeated_below_trigger_floor", sa.Integer(), nullable=True),
    )
    op.add_column(
        "review_runs",
        sa.Column("stop_rungs", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
    )


def downgrade() -> None:
    op.drop_column("review_runs", "stop_rungs")
    op.drop_column("review_runs", "repeated_below_trigger_floor")
    op.drop_column("review_runs", "new_below_trigger_floor")
    op.drop_column("review_runs", "cleared_floor")
