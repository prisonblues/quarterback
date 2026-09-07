"""How many seats the round actually asked, and how many it did not (#775)

Two integer columns on `review_runs`, holding the lengths of
`seat_routing.dispatched` and `seat_routing.held`. They exist to make a curve
tunable rather than to serve a page.

## What #775 changed

Every round dispatched the same panel. A seat that read the diff in round 1 read
the fix in round 2 with its own round-1 findings in front of it, and the cheapest
thing it could produce was more of the same shape. `panel_seats.route_seats` makes
round N's seats the set difference against round N-1's, spent within a per-round
budget: the multiplier says how many seats a round asks and the routing says
which.

Which means `reviewers_selected` stopped being the answer to two questions. It is
what `.harness-rules` and `--reviewers` CONFIGURED; what the round then put a
question to is a different, smaller set, and nothing on this board held it.

## Why these two are columns

The question they are owed is fleet-wide and archival, and it is the one nobody
can ask later: across hundreds of rounds, does a round that asked fewer seats find
less? `round_budgets.multipliers` ships `[1.0]` precisely because nobody has
measured a curve, and anybody who wants to tighten one on evidence rather than on
taste needs a population of rounds with a dispatch count beside a finding count.
The payload those counts are computed in lives in a temp directory on whichever
host ran the panel and is gone by the time anybody asks — `m099d5fb1`'s argument
for `repeats_only_locality` and `m0fc6fd4c`'s for `fix_blast_lane`, arriving at
the same place a third time.

**Both, because neither half is the measurement.** Three seats out of three and
three out of six are the same `seats_dispatched`, so the denominator has to be
stored beside it. `reviewers_selected` is not that denominator: the routing runs
over the LLM seats and that column carries the whole panel, sonarqube included.

**And neither is reconstructible from the `review_reviewers` rows.** A held seat
lands there with `ran = false`, and so does a seat whose CLI is missing, a seat
that crashed and a seat cut by a ceiling. The word that tells the four apart is
`seat_routing.why`, which is one round's working and is deliberately dropped, so
counting `ran = false` rows would give four causes one number.

`why`, `budget`, `prior_round` and `prior_dispatched` ride the same block and get
no column. They are that round's working: the per-seat reason words, the
arithmetic that produced the budget, and the round the complement was taken
against. A reader with a question about one round has that round — the payload is
published and `GET /review/findings` lays a cycle's rounds out in order — and
`budget` in particular would be one value derivable two ways with two chances to
drift. `tests/test_payload_key_drift.py` carries that decision in writing, which
is the only other way past the drift check.

## Nullable, no server default, no backfill

NULL means this round made no routing decision: the panel nulls the whole block on
every path that never got as far as choosing — a title skip, a pre-flight refusal,
a spend-ceiling refusal — and it is also every row in this table today and every
producer too old to send the block.

**Never 0**, and the direction is the whole point. `seats_held = 0` is a
MEASUREMENT — this round asked every seat it could, which is the answer every
round on the shipped flat curve gives — and it is the baseline the first repo to
opt into a curve gets compared against. A backfilled zero would fill that baseline
with rounds that never routed at all and then calibrate against it. `m099d5fb1`
refuses a backfilled zero for the same reason and `m1adf9129` refuses a backfilled
`false` for its mirror; there is no second copy to backfill from in any of the
three.

The two are NULL and non-NULL together: they come off one block the panel sends
whole.

## The CHECKs

`>= 0` each, on `ck_review_runs_repeats_*_non_negative`'s rule — the API is not
the only writer. A negative count is not a smaller measurement; it nets against a
real one, and the direction it moves the answer is the dangerous one, because a
window of routed rounds that reports nothing held back is the reading that says a
curve is safe to tighten.

One constraint per column rather than one over the pair, so a caller is told which
of the two refused it — `m1adf9129`'s argument for naming a constraint rather than
widening the one beside it.

`>= 0` and not `seats_dispatched >= 1`, deliberately. `route_seats` keeps at least
one seat whenever there is one to keep, but a panel configured with no LLM seat at
all selects none, dispatches none and holds none, and `0 / 0` is that round's
honest record rather than a fault. A floor of 1 would refuse it at the boundary
and cost the caller the round's findings, scorecards and accounts.

NULL passes on both, or the migration would be unrunnable against every row this
board holds.

## Downgrade

`downgrade()` drops both columns and **the data is gone**, on `m7fc78723`'s terms
and for its reason. Acceptable for two columns no existing read depends on, and
written down here rather than discovered.

Revision ID: m25dac5c9
Revises: m0fc6fd4c
Create Date: 2026-09-07 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m25dac5c9"
down_revision: str | None = "m0fc6fd4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_runs",
        sa.Column("seats_dispatched", sa.Integer(), nullable=True),
    )
    op.add_column(
        "review_runs",
        sa.Column("seats_held", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_review_runs_seats_dispatched_non_negative",
        "review_runs",
        "seats_dispatched >= 0",
    )
    op.create_check_constraint(
        "ck_review_runs_seats_held_non_negative",
        "review_runs",
        "seats_held >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_runs_seats_held_non_negative", "review_runs",
                       type_="check")
    op.drop_constraint("ck_review_runs_seats_dispatched_non_negative",
                       "review_runs", type_="check")
    op.drop_column("review_runs", "seats_held")
    op.drop_column("review_runs", "seats_dispatched")
