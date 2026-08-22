"""Recurrence: is a round's finding standing where the last fix was working (#67)

#48's `provenance` asks whether the previous round's fix *wrote the line* a new
finding sits on. This asks the next question along, and it is a different one:
had that fixer been sent there? A fix that patches a wrong assumption produces
the next round's findings; a fix that removes the assumption does not, and the
loop currently cannot tell those two rounds apart. It stops on a round count,
which fires at the same point whether the rounds are converging or circling.

Three per-finding columns and two per-run tallies:

* `review_findings.recurrence` — the mechanical bucket
  (`revisited` / `fix-site` / `elsewhere` / `unknown`). **The irreplaceable one**,
  for the reason 0017 gives about `provenance`: it is per finding and per round,
  so a round that runs while the column does not exist is gone. `revisited` is the
  conjunction — the previous round raised a finding in this file, the fixer wrote
  lines in this file, and this finding sits within a short radius of them — which
  is what makes it narrower than the "same file" reading #67 warns is too wide.
  It is a POSITION and not a verdict — see the calibration section below.
* `review_findings.recurs_of` — WHICH earlier finding it stands on, as that
  finding's own `finding_key`. A pointer rather than a second copy, and the thing
  that makes the bucket auditable: an uncalibrated signal that cannot be checked
  against the record it was computed from is not evidence of anything.
* `review_findings.premise_verdict` — what the JUDGE said when asked directly,
  per finding: does this finding invalidate the premise of the fix that preceded
  it (`invalidates`), is it a separate defect (`separate`), or can it not be told
  (`unclear`)? Asked because the mechanical test cannot answer it — it can see
  that a fixer was working here and not whether this finding says that fixer's
  assumption was wrong.

Both witnesses are stored, deliberately, and neither is folded into the other.
One is mechanical and one is adjudicated, and the rounds where they DISAGREE are
the rows worth a human's time; a single blended number would hide exactly those.

* `review_runs.recurrence_counts`, `review_runs.premise_counts` — the panel's own
  tallies, stored rather than derived, for the reason 0017 gives for
  `provenance_counts`: the panel counts over the findings the cycle must clear
  and these rows also include the dismissed ones, so a derivation would disagree
  with the panel's own statement about its own round. And `{}` ("the question does
  not arise") is a distinction no derivation carries.

## What the first calibration says, before anything is built on it

The mechanical column was replayed over 36 rounds from 26 pull requests — every
multi-round cycle this board holds — against #67's own three cases (#61, #29,
#88) and every other cycle as the control:

    narrowing                     #61/#29/#88      the rest
    file + within 20 lines            83%            69%
    file + within 5 lines             79%            64%
    file + exactly on a line          65%            52%
    ...and prior finding within 20    29%            27%

It does not separate them, at any radius, and tightening the rule lowers both
columns together. The reason is legible in the runs: under #41's increment scope
a later round REVIEWS the fix commit, so a new finding at the fix's site is the
ordinary case rather than the exceptional one.

That result is why the column is called `revisited` and not `circling`, and why
`premise_verdict` is here beside it rather than as a later refinement — #67's own
comment on PR #88 argued the grouping key would have to be "not 'same file' but
'same way of being wrong'", and this is the measurement that agrees. The
mechanical rate is kept because a rate that saturates is itself a fact about the
loop and is the baseline any later rule has to beat, not because it works.

## Nothing gates on any of it, and that is the design

#67 asks for the instrument before the gate and says why in one line: two pull
requests in one day is an observation, not a calibrated rule. So `round_stop`
does not read these columns, no ceiling in `panel_caps` reads them, and no fix
pass is skipped because of one. They are recorded, tallied and printed. A few
dozen cycles of them are what would justify wiring them to anything — and the
`recurs_of` pointer is what would let somebody check the labels before they did.

## Nullable, and null means NOT RECORDED

Never "does not recur". The same three-way distinction 0017 draws: a pre-v2.96
run has none because nothing stored it; a round 1 has none *because the question
does not arise*; `unknown` is the real bucket for a finding that was asked about
and could not be placed. No scorecard counters here, unlike 0017 — this is a
property of a ROUND's relationship to the round before it, not a competency of
the reviewer that happened to raise the finding, and a per-reviewer tally of it
would be a leaderboard axis nobody has argued for.

The revision id is opaque and carries no ordering (#341): this is schema revision
**med8bb81b**, and its place in the chain is `down_revision` and nothing else.

Revision ID: med8bb81b
Revises: mdee05a89
Create Date: 2026-08-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "med8bb81b"
down_revision: str | None = "mdee05a89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No index on any of them, for the reason 0017 records for `provenance`:
    # every read reaches findings BY RUN, and the window aggregate in
    # `/review/stats` groups a set the run join has already narrowed. An index
    # here would be write cost on the largest table this feature touches, for a
    # scan nothing performs.
    op.add_column("review_findings", sa.Column("recurrence", sa.Text(), nullable=True))
    op.add_column("review_findings", sa.Column("recurs_of", sa.Text(), nullable=True))
    op.add_column("review_findings",
                  sa.Column("premise_verdict", sa.Text(), nullable=True))

    # The vocabularies, at the boundary rather than only in the API — the rule
    # `ck_review_findings_needs_human_class` follows one column over. `provenance`
    # (0017) has no such CHECK and predates the convention; these do, because both
    # are COUNTED and a value outside the vocabulary silently leaves the numerator
    # while still counting as coverage.
    op.create_check_constraint(
        "ck_review_findings_recurrence",
        "review_findings",
        "recurrence IS NULL OR recurrence IN "
        "('revisited', 'fix-site', 'elsewhere', 'unknown')",
    )
    op.create_check_constraint(
        "ck_review_findings_premise_verdict",
        "review_findings",
        "premise_verdict IS NULL OR premise_verdict IN "
        "('invalidates', 'separate', 'unclear')",
    )
    # One-directional, not a biconditional, and the asymmetry is the point. A
    # `recurs_of` under any other bucket names a circle the measurement did not
    # find — evidence for a judgement nobody made, the shape
    # `ck_review_findings_needs_human_evidence` refuses. The other direction is
    # left open: a `revisited` that names no earlier key is incomplete rather than
    # false, and a producer too old to send the pointer should still be able to
    # send the bucket.
    # `IS NOT DISTINCT FROM`, not `=`, and this is the SQL trap that makes the
    # difference between a rule and a decoration. A CHECK passes when it evaluates
    # to NULL as well as when it evaluates to true, and `recurrence = 'revisited'`
    # is NULL for every row whose `recurrence` is NULL — which is most of them, and
    # is exactly the row this refuses. Written with `=` the constraint accepted a
    # pointer attached to no measurement at all: the one case it exists to stop.
    # Caught by a test that tried the write rather than by reading the predicate.
    op.create_check_constraint(
        "ck_review_findings_recurs_of_revisited",
        "review_findings",
        "recurs_of IS NULL OR recurrence IS NOT DISTINCT FROM 'revisited'",
    )

    op.add_column("review_runs",
                  sa.Column("recurrence_counts", postgresql.JSONB(), nullable=True))
    op.add_column("review_runs",
                  sa.Column("premise_counts", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "premise_counts")
    op.drop_column("review_runs", "recurrence_counts")
    op.drop_constraint("ck_review_findings_recurs_of_revisited", "review_findings")
    op.drop_constraint("ck_review_findings_premise_verdict", "review_findings")
    op.drop_constraint("ck_review_findings_recurrence", "review_findings")
    op.drop_column("review_findings", "premise_verdict")
    op.drop_column("review_findings", "recurs_of")
    op.drop_column("review_findings", "recurrence")
