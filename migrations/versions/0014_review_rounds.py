"""v2.15: rounds, coverage declarations, and the re-review flag

A panel run recorded what was found and never what the run could not see. Both
halves of that are invisible in the old schema: a reviewer handed 60k of a 118k
diff reported confidently on the half it read and its row looked like everyone
else's, and a reviewer that could not judge an area had no way to say so — a
finding count reports "clean" and "I could not tell" as the same zero.

The run also had no place in a sequence. `/panel-review-pr` fixed and pushed
without re-reviewing, so the fixer's own commit was read by nobody; now that a
second round exists, "what did THIS round find that the last one had not" and
"what stopped the loop" are facts worth keeping — a round cap reached with work
outstanding is not the same event as a dry round, and only one of them is
convergence.

Existing rows take round 1 and NULL declarations: they were never asked, and
defaulting them to "nothing to declare" would let pre-v2.15 runs read as
earned-clean, which is the exact confusion this release exists to remove.

The revision number and the release number are unrelated counters: this is schema
revision **0014** and it ships in product version **v2.15**. (v2.14 is the release
that moved the panel's merge into its judge and changed no schema.)

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("coverage_note", sa.Text(), nullable=True))
    # NOT NULL with a default: every run that exists was a first round, and that
    # is a fact rather than an unknown.
    op.add_column(
        "review_runs",
        sa.Column("round", sa.Integer(), server_default="1", nullable=False),
    )
    # Which cycle the round belongs to. Nullable: no run recorded before this
    # migration was part of a cycle anything could name, and inventing one would
    # let a positional guess read as a stated fact.
    op.add_column("review_runs", sa.Column("cycle", sa.Text(), nullable=True))
    # Nullable on purpose: "the panel never said" is not "nothing new".
    op.add_column("review_runs", sa.Column("new_findings", sa.Integer(), nullable=True))
    # `stopped` is the panel's own boolean. Without it the reason string doubles
    # as the answer, and a round that said "go again" reads as one that stopped.
    op.add_column("review_runs", sa.Column("stopped", sa.Boolean(), nullable=True))
    op.add_column("review_runs", sa.Column("stop_reason", sa.Text(), nullable=True))
    op.add_column("review_runs", sa.Column("stop_confident", sa.Boolean(), nullable=True))
    # The reasons a stop was unearned, verbatim. `stop_confident` says a clean
    # verdict was not evidence; this says why, which is what a reader needs.
    op.add_column(
        "review_runs",
        sa.Column("stop_veto", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.add_column(
        "review_reviewers",
        sa.Column("could_not_assess", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # A reviewer whose reply did not parse lands on could_not_assess NULL, which
    # is also where a member that was never asked lands. Only this tells them
    # apart, and an unparsed reviewer is a coverage failure the stats must see.
    op.add_column("review_reviewers", sa.Column("unstructured", sa.Boolean(), nullable=True))
    op.add_column(
        "review_reviewers",
        sa.Column("rereview_flagged", sa.Integer(), server_default="0", nullable=False),
    )

    op.add_column(
        "review_findings",
        sa.Column("needs_rereview", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("review_findings", sa.Column("new_this_round", sa.Boolean(), nullable=True))
    op.add_column(
        "review_finding_reports",
        sa.Column("needs_rereview", sa.Boolean(), server_default="false", nullable=False),
    )

    # The API coerces these three (round to at least 1, the counts to non-negative
    # integers), but it is not the only writer: a migration, a backfill script or a
    # psql session can put a round 0 or a negative count in, and both break run
    # ordering and the published per-reviewer statistics. Checked in the schema so
    # the guarantee holds for whoever writes.
    op.create_check_constraint("ck_review_runs_round_positive", "review_runs",
                               '"round" >= 1')
    op.create_check_constraint("ck_review_runs_new_findings_non_negative", "review_runs",
                               "new_findings >= 0")
    op.create_check_constraint("ck_review_reviewers_rereview_flagged_non_negative",
                               "review_reviewers", "rereview_flagged >= 0")


def downgrade() -> None:
    op.drop_constraint("ck_review_reviewers_rereview_flagged_non_negative",
                       "review_reviewers", type_="check")
    op.drop_constraint("ck_review_runs_new_findings_non_negative", "review_runs",
                       type_="check")
    op.drop_constraint("ck_review_runs_round_positive", "review_runs", type_="check")
    op.drop_column("review_finding_reports", "needs_rereview")
    op.drop_column("review_findings", "new_this_round")
    op.drop_column("review_findings", "needs_rereview")
    op.drop_column("review_reviewers", "rereview_flagged")
    op.drop_column("review_reviewers", "unstructured")
    op.drop_column("review_reviewers", "could_not_assess")
    for col in ("stop_veto", "stop_confident", "stop_reason", "stopped", "new_findings",
                "cycle", "round", "coverage_note"):
        op.drop_column("review_runs", col)
