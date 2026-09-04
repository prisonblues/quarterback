"""Whether a round was primed by the PR's own words (#550, under #621)

PR #631 gave the reviewer prompt the pull request's title and body, framed as the
author's assertion rather than as fact. Before it the prompt carried five keys —
number, repo, base, the CI brief, the diff — and none of them was the claim, so the
class of defect "this change asserts a measured result and ships nothing that
produces it" was invisible to the panel by construction.

#550 shipped that mechanism with its own condition unmet, and said so at the time.
Handing a reviewer a body that says "this is safe because X" **primes** it to accept
X. A primed seat reports FEWER findings. Fewer findings look like a clean PR. So the
framing is not something to assert — it is something to measure, by running the same
PRs with and without the block and comparing the counts. If the seats go quieter, the
block is worse than nothing.

That measurement was blocked on two things, and the second is this revision.

## What the record could not do

The panel already knew whether it sent the block. It said so in `config_notes` — a
list of English sentences on the round payload, stored in this board's `rules`/notes
tier and read by humans. Nothing on `review_runs` carried the fact, so a query over a
population of rounds could not partition them by whether they were primed. Both arms
of the experiment could be run and the comparison would still be unaggregatable,
which is the same shape of defect `mc57fb9ba` fixed for the dials and `mdef4716b` for
the harness: a fact the round knew about itself, kept only in prose, and therefore
lost to every reader that is not a person.

The other blocker was that no dial turned the block off, so the control arm could not
be produced at all. That half ships in the harness (`review_panel.pr_claim`, and
`panel.py --no-pr-claim` for one run).

## Two columns, and the split is the judgement

* `pr_claim` (Boolean) — what the round **asked for**. This is the ARM a round
  belongs to: the repo's `review_panel.pr_claim`, or the per-run `--no-pr-claim`.
* `pr_claim_sent` (Boolean) — what the seats **got**. The block is charged against
  the tightest seat's diff budget and dropped whole where that budget cannot carry
  `PR_CLAIM_MIN_CHARS` of the author's own words, so a round can ask and still send
  nothing.

One boolean would have been cheaper and would have confounded the thing it was added
for. A round that asked for the block and dropped it to the budget floor is in
NEITHER arm and has to be excluded; a reader told only "the seats saw no claim" would
score it as a control, and score a budget effect as evidence about the framing. That
is precisely the error the measurement exists to avoid making.

It is also the split `0024` already made for `code_access` — the setting, kept apart
from what actually reached the seats — and its argument transfers without change: a
round with the setting on and nothing delivered is a configuration doing nothing,
which is visible in the difference and invisible in either column alone.

The JUDGE's copy is deliberately not a third column. A judge is never shown a claim
the seats were not, so it is downstream of `pr_claim_sent`; the population being
compared is the seats'; and where the judge's own budget cannot carry the block the
round says so in `config_notes`, which is where a fact about one round's adjudication
belongs.

## Uninterpreted, unindexed

Stored as sent. This board does not know what priming is, does not read the dial that
produced the setting, and does not check the two against each other — `pr_claim:
false` with `pr_claim_sent: true` is a shape this producer does not send and a
constraint written against today's producer is a 500 waiting for tomorrow's, which is
`mdef4716b`'s reasoning about CHECKs arriving again.

Not indexed. Every query on this table is already selective on `(repo, pr)` or `ts`;
a population is partitioned in the application after the window has been narrowed,
and an index nothing filters on is a write cost paid by every round.

## Nullable, no server default, no backfill

NULL is "the panel did not say" on both — every row in this table today, and every
skip and refusal path permanently. A round that dispatched no seat did not decline to
prime one, and writing `false` there would put a round that reviewed nothing into the
unprimed arm of a comparison it was never in.

**A backfill would be the bug**, on `mdef4716b`'s terms. The only value available to
write is today's setting, the block landed mid-population (#631, 2026-08-31), and
attributing today's arm to rounds that ran under the other one is exactly the mixing
these columns exist to make visible.

## Downgrade

`downgrade()` drops both and the data is gone. There is no second copy that survives:
the payload it came from is a temp file on whatever host ran the panel, and the
round's own `config_notes` are prose that no query can partition on — which is the
whole reason this revision exists.

Revision ID: md3a02ab5
Revises: mdef4716b
Create Date: 2026-09-01 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "md3a02ab5"
down_revision: str | None = "mdef4716b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("pr_claim", sa.Boolean(), nullable=True))
    op.add_column("review_runs",
                  sa.Column("pr_claim_sent", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "pr_claim_sent")
    op.drop_column("review_runs", "pr_claim")
