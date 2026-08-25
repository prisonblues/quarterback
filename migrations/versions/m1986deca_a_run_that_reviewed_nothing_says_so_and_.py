"""A run that reviewed nothing says so, and keeps its file list (#94)

`panel.py`'s title-pattern skip — merges, promotes, format-the-world commits —
builds a complete payload and returns before `record_run` is ever called. That
was deliberate and right on its own terms: no review happened, and recording one
would be a non-event stored as an event. The cost was paid somewhere else. The
board never learned which files those PRs touched, so `GET /review/collisions`
saw a skipped PR as **neither subject nor rival** — blind in both directions,
and blind precisely on the changes that touch the most files and are most often
merged unattended.

So the run gets recorded, and it is made to tell the truth about itself instead
of being kept out.

`reviewed` is the truth. Three states, and the third is what makes the column
safe to add to a table that already has 289 rows in it:

* `True` — a panel ran.
* `False` — this run reviewed nothing and says so. The title skip and the
  pre-flight refusal both exit here. The row exists for what it MEASURED — the
  PR's changed-file list, its state, the head it moved to — not for a verdict it
  never reached.
* `NULL` — nobody said, which is every row recorded before today. Not `True`.
  The refusal path has been sending `reviewed: false` and being recorded for
  several releases while the board discarded the field, so those rows are
  already non-reviews and nothing on them says which they are. Defaulting the
  column to `True` would make a brand-new column knowingly wrong about a known
  class of row, and this repo's whole complaint about its own data is answers
  that read safer than the evidence supports.

Hence **nullable, no server default, no backfill**. A backfill would have to
guess, and the two directions are not symmetrical: guessing `True` certifies
runs nobody can vouch for, and guessing `False` from an all-`ran: false`
scorecard would move numbers the /panel leaderboard has already published.

The reading rule the application code is written to, stated here because a
column's meaning outlives whichever query happens to be reading it this year:

* a question whose wrong answer is a false all-clear asks `reviewed IS TRUE`;
* a count that exists to match what has already been published asks
  `reviewed IS NOT FALSE`, which keeps every legacy row exactly where it has
  always been and excludes only runs that state outright that they reviewed
  nothing.

`skip_reason` is the sentence beside the predicate — "title matches skip pattern
/^Merge /" — because a human deciding whether a 400-file merge is worth reading
by hand wants the reason, not a boolean. The panel has sent it on both non-review
exits all along; only the board's ingest lacked a field to put it in.

The CHECK is the one combination that must be unrepresentable: a run that
reviewed nothing cannot also have earned a confident stop. `stop_confident` is
what `preland --require-earned-stop` reads and what the review queue calls
convergence, so `reviewed = false` beside `stop_confident = true` would be a
non-event certifying a pull request as done. At the boundary and not only at
ingest, for the reason `ck_review_runs_round_positive` gives: the API is not the
only writer, and a write path added later must not be able to reintroduce it
quietly. NULL on either side passes — that is every legacy row, and every round
whose panel did not say.

Neither column is indexed, deliberately. `reviewed` has at most three values over
a table whose every query is already selective on `(repo, pr)` or `ts`, both of
which are indexed; a boolean index here would be write cost on the review path
for a scan Postgres would decline to use.

Revision ID: m1986deca
Revises: mef441e81
Create Date: 2026-08-25 05:53:28.751964

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m1986deca"
down_revision: str | None = "mef441e81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("reviewed", sa.Boolean(), nullable=True))
    op.add_column("review_runs", sa.Column("skip_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_review_runs_unreviewed_not_confident",
        "review_runs",
        "NOT (reviewed IS FALSE AND stop_confident IS TRUE)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_review_runs_unreviewed_not_confident", "review_runs", type_="check"
    )
    op.drop_column("review_runs", "skip_reason")
    op.drop_column("review_runs", "reviewed")
