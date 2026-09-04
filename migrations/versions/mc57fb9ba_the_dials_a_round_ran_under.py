"""The dials a round ran under, beside the verdict they produced (#643)

The panel has published `review_panel` — the eight-going-on-twelve #165/#297
dials **as it applied them** — on every reviewed round since #165. `ReviewIn` is
declared `populate_by_name=True` with no `extra=`, so pydantic's default
`extra="ignore"` applied and ingest dropped it, exactly as it dropped `head_sha`,
`unread_files`, the provenance pair and `converged` before it. This is the fifth
instance of one bug, and #643's drift test is what stops there being a sixth.

## Why this one, and why now

`converged` (m6bc45ff1) records whether a round was a clean finish. Its argument
for being a stored column rather than a board-side derivation is that two of its
conjuncts are cut at `cleared_floor`, which is `round_trigger_floor` or
`fix_severity_floor` depending on whether a budget is in force — repo dials, and
this board neither stores them nor knows what they mean. That argument is sound
and it has a corollary nobody wrote down: a reader handed `converged` today
cannot check it against anything, because the row does not carry the policy it
was decided under.

All three inputs to `cleared_floor` are in `review_panel`. So this column does
not make the stored answer unnecessary — a stored answer still beats a
reconstruction — it makes the stored answer **auditable**, which is a different
and previously unavailable thing.

"Now" is not a preference either. The round payload lives in a temp directory on
whatever host ran the panel and is not kept, so a dial set that is not recorded
while a round runs is not recoverable afterwards. Every round that ran under
#165 has already lost its dials that way.

## Opaque, unindexed, uninterpreted

Stored as JSONB and never read by this board for meaning. `app/api/dials.py`
makes that case at length: a second place that knew what `review_panel.max_rounds`
was is the drift #305 exists to end. Nothing here validates a dial name, coerces
a value, or derives anything from one; ingest bounds the object's SIZE and stores
what arrived.

Not indexed. Every query on this table is already selective on `(repo, pr)` or
`ts`, both indexed, and no read path filters on a dial — nor should one, since
that would require knowing what a dial means.

## Nullable, no server default, no backfill

Three states, on the rule `reviewed` and `converged` already state:

* an object — the dials this round applied.
* `{}` — a caller that sent an empty object. Kept apart from NULL for the reason
  `provenance_counts` keeps them apart.
* NULL — the panel did not say. That is every row in this table today; it is
  also, permanently, every skip and every pre-flight refusal, because those paths
  resolve a review policy and never apply one, and the panel sends `null` there
  on purpose.

A backfill would have to guess a policy, and the guess would be *today's* dials
attributed to rounds that ran under whatever was written months ago — which is
precisely the incident #305 was filed over, manufactured rather than merely
unanswerable.

## No CHECK

`converged` earned a constraint because one combination of its value and its
neighbours is incoherent by construction. There is no such combination here: this
board does not know what any dial means, so it has no proposition about them to
enforce, and a constraint written in terms of a dial's name would be the board
learning the vocabulary it must not learn.

## Downgrade

`downgrade()` drops the column and **the data is gone**, on the same terms
m6bc45ff1 states: there is no second copy, the payload it came from is a temp
file on another host, and re-upgrading starts the population again from empty.
Acceptable for a column no existing read depends on, and unacceptable to do
casually.

Revision ID: mc57fb9ba
Revises: m6bc45ff1
Create Date: 2026-08-31 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "mc57fb9ba"
down_revision: str | None = "m6bc45ff1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_runs",
        sa.Column("review_panel", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("review_runs", "review_panel")
