"""What the cycle left behind, and who owes it (#717)

`preland` decides whether a pull request may be merged, and one of its clauses
was `elif confirmed: HOLD`. Grep the file for `severity`, `floor`, `P2` or `P3`
and there are no matches: any nonzero judge-confirmed count was a reason to
hold, whatever the findings were.

That contradicts `review_panel.fix_severity_floor`, which exists to say *these
are reported and not fixed here* (#165). A repository that raises the floor to
P2 has declared its P3s and P4s non-blocking; the panel prints them under their
own heading and hands them to nobody; `preland` then held the merge on them
anyway. The practical effect is that such a repo can essentially never reach
READY, because a round asked to find problems almost always finds a P3.
Measured on lexray#1631: round 2 stopped with `handed_to: "nobody"` and eleven
below-floor findings, every one recorded `deferred` on this board, and the gate
returned HOLD on "11 judge-confirmed finding(s) are unresolved".

## Why the fix is a column and not a subtraction

#42 has published the answer on every round payload since it landed:
`round_stop.outstanding` carries `fixable`, `below_floor` and `escalated` as
separate lists, and `handed_to` as the round's own verdict on whether anything
is owed. `qb record-review` POSTs the whole block. `ReviewIn` is declared
`populate_by_name=True` with no `extra=`, so pydantic's default `extra="ignore"`
applied and ingest dropped it — the same silent drop this directory records for
`head_sha`, `unread_files`, the provenance pair and `converged` (`m6bc45ff1`),
in the same file, for the same reason. The severity split exists on every round
the panel has ever run and was recoverable from none of them.

So the obvious repair — subtract a below-floor count from `n_confirmed` — is
the one thing this must not do, on two counts:

* **the populations are different.** `n_confirmed` is every confirmed finding on
  the round. The disposal is computed over `work`, which is that set minus the
  keys an outcome already cleared, minus the escalation register, minus what a
  fix pass narrowed. A difference between the two is not a count of anything.
* **`escalated` must keep blocking at any severity**, and an escalated finding
  can be below the floor. `fixable + escalated` is a sum this board may take
  because `panel_rounds.round_stop` removes the escalated and narrowed keys
  before splitting the rest at the floor, so the three sets are disjoint by
  construction (`harness/loops/tests/test_panel_outstanding.py` pins it). A
  subtraction from `n_confirmed` would quietly clear escalations a human owes an
  answer on.

## Counted here, never split here

`outstanding_counts` is the LENGTH of each list the panel published and nothing
else. This board does not decide which finding is below which floor: the floors
are repo dials it holds as opaque JSON and does not interpret, which is
`m6bc45ff1`'s argument for `converged` and `app/api/dials.py`'s at length. A
board-side split would be a second reading of the policy that produced the
verdict stored beside it, free to disagree with the panel about the same round.

Counts rather than the key lists, and rather than a `counts` block the panel
sends beside them: `len()` of a list cannot disagree with that list, whereas a
sibling tally in the same payload can. The keys themselves stay off this row for
`unread_files`' reason — `GET /reviews?limit=500` would serialise five hundred
finding lists — and they are already on the round's own findings.

## Nullable, no server default, no backfill

Two states with a hard rule between them:

* an object — the round said, and `fixable + escalated` is what it is owed;
* NULL — the panel did not say. Every row in this table today, every producer
  too old to send the block, and every payload whose block arrived without all
  three of the counted-and-read keys, which ingest refuses whole (half a
  disposal is not a smaller one, it is a different one).

**NULL is not zero, and the consumer is written that way.** `preland` falls back
to its old reading — HOLD on `n_confirmed` — for a round with no split, which is
exactly what a merge gate should do with an unknown number of unresolved
findings. A backfill would have to guess the split from a count that does not
contain it, and every missing input reads as "nothing outstanding": the
flattering direction, on the one field that decides whether a pull request may
be merged.

`handed_to` is the verdict beside the measurement, on the terms `round_stop`
keeps them apart: the counts are true of a round either way, and `handed_to` is
null on a round that is going again and making no disposal. It is stored for the
reader of a verdict, not gated on — `preland` rules on the counts, which cannot
drift from the lists they were taken from.

## Downgrade

`downgrade()` drops both columns and **the data is gone**. There is no second
copy: the round payload lives in a temp directory on whatever host ran the
panel. That is acceptable for columns no existing read depends on, and it is
written down here rather than discovered.

Not indexed, deliberately, on `converged`'s argument: every query on this table
is already selective on `(repo, pr)` or `ts`, and nothing aggregates over these.

Revision ID: m7fc78723
Revises: mc0d7f83a
Create Date: 2026-09-03 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "m7fc78723"
down_revision: str | None = "mc0d7f83a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_runs",
        sa.Column("outstanding_counts", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
    )
    op.add_column("review_runs", sa.Column("handed_to", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "handed_to")
    op.drop_column("review_runs", "outstanding_counts")
