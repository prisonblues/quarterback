"""A repeat is where a finding landed, and the counts say whether that was worth it (#771)

Two integer columns on `review_runs`, and they exist to settle an argument rather
than to serve a page.

## What #771 changed

Cross-round finding identity used to be `finding_key` — a hash of the reviewing
model's own WORDING. A defect the panel restated in different words on the next
round was therefore a new finding: it bought a round it had already bought, it
landed in `escalate_on.fix_injection` as fresh damage, and it never aged. #771
matches findings by LOCALITY instead — the finding's line range against the
round's diff hunks, within a slack — and the panel now computes repeats both ways
and passes the UNION to `round_stop`.

Which makes the change invisible from the outside. Every downstream number is
computed off the union, so a locality matcher that caught nothing and a locality
matcher that caught half this cycle's repeats produce the same row. The only way
to tell them apart is to count, and the count has to survive the round.

## Why these two are columns

`locality_repeats.only_locality` is how many of a round's repeats ONLY the
locality matcher recognised — the ones a wording hash missed. `by_key` is the
population the old comparison already had, and the two are DISJOINT by
construction: the panel subtracts the key matches out of the locality matches
before counting either. So the pair carries a sum and a sum does not carry the
pair, which is `mb79c396d`'s argument for two below-trigger-floor columns rather
than one total, and it applies here with more force — the RATIO is the whole
measurement.

Bound and stored rather than listed as dropped, on `m1adf9129`'s terms for
`attested`. The verdict this feature is owed is "over a few dozen cycles, did the
locality matcher recognise a non-trivial share of the repeats" — a query over
hundreds of rounds across every PR the fleet reviews. The round's own published
JSON cannot answer it: it lives in a temp directory on whichever host ran the
panel and is gone by the time anybody asks. A fleet where `only_locality` stays
at zero is a fleet where the matcher should come out, and without these columns
that decision would be made on somebody's recollection of a report.

`keys` and `why` ride the same block and get no column. `keys` is the finding
names behind `only_locality` — per-round detail, already in the payload a reader
can fetch, and an unbounded list per run for nothing the count does not already
say; `why` is one round's prose about a comparison it could not make.
`tests/test_payload_key_drift.py` carries that decision in writing, which is the
only other way past the drift check.

## Nullable, no server default, no backfill

NULL means the panel did not say — every row in this table today, and every
producer too old to send the block. **Never 0**, and the direction is what
matters: "the locality matcher caught nothing extra" is the FINDING this
measurement exists to report, and a backfilled zero would fill the population
with rounds that never ran the matcher and build the case against it out of them.
`m1adf9129`'s `attested` refuses a backfill for the mirror-image reason, and there
is no second copy to backfill from in either case.

The two are NULL and non-NULL together: they come off one block the panel sends
whole.

## The CHECKs

`>= 0` each, on `ck_review_runs_new_findings_non_negative`'s rule — the API is not
the only writer. A negative count is not a smaller measurement; it would net
against a real one and make the ratio read LOW, which is the direction that gets
the feature deleted. One constraint per column rather than one over the pair, so a
caller is told which of the two refused it — the argument `m1adf9129` makes for
giving `ck_review_runs_converged_implies_attested` its own name instead of
widening the constraint beside it.

## Downgrade

`downgrade()` drops both columns and **the data is gone**, on `m7fc78723`'s terms
and for its reason. Acceptable for two columns no existing read depends on, and
written down here rather than discovered.

Revision ID: m099d5fb1
Revises: m1adf9129
Create Date: 2026-09-06 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m099d5fb1"
down_revision: str | None = "m1adf9129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_runs",
        sa.Column("repeats_only_locality", sa.Integer(), nullable=True),
    )
    op.add_column(
        "review_runs",
        sa.Column("repeats_by_key", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_review_runs_repeats_only_locality_non_negative",
        "review_runs",
        "repeats_only_locality >= 0",
    )
    op.create_check_constraint(
        "ck_review_runs_repeats_by_key_non_negative",
        "review_runs",
        "repeats_by_key >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_runs_repeats_by_key_non_negative", "review_runs",
                       type_="check")
    op.drop_constraint("ck_review_runs_repeats_only_locality_non_negative",
                       "review_runs", type_="check")
    op.drop_column("review_runs", "repeats_by_key")
    op.drop_column("review_runs", "repeats_only_locality")
