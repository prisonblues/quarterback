"""How dangerous the repair was (#770), as one word per round

One text column on `review_runs`, holding `round_stop.fix_blast.lane`: `low`,
`medium` or `high`.

## What it is

A finding is graded on severity and nothing else, so a fixer is told how bad the
DEFECT is and never how far the REPAIR reaches. `harness/loops/panel_blast.py`
classifies the fix pass's own changed paths against a rule table — migrations,
auth, secrets, irreversible infrastructure, dependencies, public API surface,
source with no test beside it — and the lane is the worst category that fired.
Nothing on this board could say any of that before.

## Why it is a column

It is the first input to #770's `fix_risk` axis, and the axis is calibrated
against a correlation nobody can compute today: across hundreds of rounds, do fix
passes that land in dangerous code write more of the NEXT round's findings than
ones that touch a docstring. That is a query over a population of rounds, and the
payload the lane is computed in lives in a temp directory on whichever host ran
the panel and is gone by the time anybody asks — `m099d5fb1`'s argument for
`repeats_only_locality`, and `m1adf9129`'s for `attested`, arriving at the same
place for the third time.

`categories`, `evidence` and `reason` ride the same block and get no column. They
are one round's WORKING: the categories that fired, the file that fired each, and
a sentence assembled out of the two. A reader with a question about one round has
that round — the payload is published and `GET /review/findings` lays a cycle's
rounds out in order — and storing them would cost an unbounded structure per run
for nothing the lane does not already say. `tests/test_payload_key_drift.py`
carries that decision in writing, which is the only other way past the drift
check.

## Nullable, no server default, no backfill

NULL means the fix surface could not be measured: the producer nulls the whole
block there, and it is also every row in this table today and every producer too
old to nest the key. **Never `low`**, and the direction is the whole point — "the
pass opened no dangerous file" and "nobody measured" are different claims, and a
backfilled `low` would fill the axis's safe half with rounds that never looked and
then calibrate against it. `m099d5fb1` refuses a backfilled zero for that reason
and `m1adf9129` refuses a backfilled `false` for its mirror; there is no second
copy to backfill from in any of the three.

## The CHECK

`fix_blast_lane IN ('low', 'medium', 'high')`, and the vocabulary is SPELLED OUT
rather than imported: a migration is a frozen artefact and may not import live app
code (#344, `tests/test_migrations_self_contained.py`), so this literal is the
vocabulary AS OF THIS REVISION and a fourth lane alters the constraint in a
migration of its own — `mb3e5f721`'s rule for the `chore` class.

A closed set, where `cleared_floor` two columns over deliberately has none, and
the two do not conflict. A severity floor is a repo dial whose spellings grow, and
an unrecognised one stored verbatim gives a consumer an extra group it can SEE.
These three are an ordered scale whose consumers branch on the word: a fourth
would not appear as a fourth group, it would fall through every branch and be read
as whichever the `else` is. Ingest already coerces an unrecognised lane to NULL,
so this constraint is unreachable from the endpoint — which is why it is here. The
API is not the only writer, and a write path added later must not be able to
introduce a fourth lane quietly. NULL passes, or the migration would be unrunnable
against every row this board holds.

## Downgrade

`downgrade()` drops the column and **the data is gone**, on `m7fc78723`'s terms
and for its reason. Acceptable for one column no existing read depends on, and
written down here rather than discovered.

Revision ID: m0fc6fd4c
Revises: m099d5fb1
Create Date: 2026-09-06 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m0fc6fd4c"
down_revision: str | None = "m099d5fb1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("fix_blast_lane", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_review_runs_fix_blast_lane",
        "review_runs",
        "fix_blast_lane IN ('low', 'medium', 'high')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_runs_fix_blast_lane", "review_runs", type_="check")
    op.drop_column("review_runs", "fix_blast_lane")
