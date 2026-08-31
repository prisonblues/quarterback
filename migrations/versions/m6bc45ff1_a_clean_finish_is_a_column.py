"""A clean finish is a column, not an inference (#626)

#631 taught the panel to say, in one boolean, whether a round was a **clean
finish** rather than a cap, a veto or a policy stop with real findings left
behind. `round_stop` computes `converged` from `confident`, on purpose and in
one place, so that the two cannot disagree. It then published it on the round
payload and nowhere else.

`qb record-review` has been POSTing it ever since. `ReviewIn` is declared
`populate_by_name=True` with no `extra=`, so pydantic's default `extra="ignore"`
applied and ingest dropped it on the floor — the same silent drop this module's
v2.26 note records for `head_sha`, `unread_files` and the provenance pair, in
the same file, for the same reason. So the field the convergence epic is judged
on exists on every round #631 has run and is recoverable from none of them.

## Why a column rather than a derivation

The board already stores `stopped`, `stop_confident` and `stop_veto`, and the
temptation is to say a converged round is a stopped, confident round with
nothing outstanding. It is not, and the gap is not a rounding error:

* a **below-floor policy stop** is `stopped: true`, `stop_confident: true` and
  `converged: false`. The cycle ended with real findings outstanding that this
  repo's policy says are reported and not fixed here (#165). It is a legitimate
  ending and a landable PR; it is not a clean finish, and a metric that counted
  it would be counting unfixed work as convergence.
* the remaining conjuncts — was anything left a fix pass could take, was
  anything left under the cleared floor, is an escalation being held — are cut
  at `round_trigger_floor` and `cleared_floor`. Those are repo dials, and this
  board stores dials as opaque JSON and does not know what any of them mean
  (`app/api/dials.py` argues that at length; a second place that knew what
  `review_panel.max_rounds` was is the drift #305 exists to end).

A board-side derivation would therefore be a reimplementation of a policy the
board cannot read, running against rows that do not carry the floors it was
decided under. It would be free to disagree with the panel about the same
round — and the direction it would drift in is the flattering one, because
every missing input reads as "nothing outstanding". A convergence number that
can quietly disagree with the panel's own answer is worse than no convergence
number, so what ships is the panel's answer, stored.

## Nullable, no server default, no backfill

Three states, exactly as `reviewed` has them and for the same argument:

* `true` — a clean finish.
* `false` — the round stopped and it was not one, or it went again.
* `NULL` — the panel did not say. That is every row in this table today,
  including every round #631 itself has run: they sent the field and the board
  discarded it, so nothing on those rows says which they were.

A backfill would have to guess, and the two guesses are not symmetrical.
Guessing `true` from `stopped AND stop_confident AND n_confirmed = 0` certifies
as clean finishes exactly the below-floor policy stops that are the interesting
counter-example, and would publish a convergence rate nobody measured. Guessing
`false` marks rounds that did converge as failures. NULL is neither, and
`GET /review/convergence` reports those rounds under `unmeasured` — outside both
sides of the ratio — rather than folding them into a bucket they are not.

## The CHECK

One combination must be unrepresentable: a round claiming a clean finish while
its own stop record says it never stopped, or says the stop was not evidence.
`converged` is computed FROM `confident` in `round_stop`, so a converged round
is a stopped and earned round by construction; a row where it is not was
written by something that did not go through the panel.

At the boundary and not only at ingest, on the rule
`ck_review_runs_round_positive` states: the API is not the only writer.
`POST /review` coerces an incoherent pair and names the drop in its response, so
this constraint is unreachable through the endpoint — which is what it is for.

NULL on any side passes: `converged IS NULL` is every existing row, and a NULL
`stopped` or `stop_confident` is a panel that never spoke. A constraint that
refused those would make this migration unrunnable rather than make the rows
honest.

## Downgrade

`downgrade()` drops the constraint and then the column, and **the data is gone**.
There is no second copy: the round payload lives in a temp directory on whatever
host ran the panel, and nothing else on this board records a clean finish. A
downgrade past this revision therefore does not merely hide the metric, it
destroys every observation of it taken since the upgrade, and re-upgrading
starts the population again from empty. That is acceptable for a column no
existing read depends on and unacceptable to do casually; it is written down
here rather than discovered.

Not indexed, deliberately. Like `reviewed`, it is a three-valued boolean over a
table whose every query is already selective on `(repo, pr)` or `ts`, both
indexed. `GET /review/convergence` groups over a window cut by those, and a
boolean index would be write cost on the review path for a scan Postgres would
decline to use.

Revision ID: m6bc45ff1
Revises: mb624cb39
Create Date: 2026-08-31 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m6bc45ff1"
down_revision: str | None = "mb624cb39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("converged", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "ck_review_runs_converged_implies_earned_stop",
        "review_runs",
        "NOT (converged IS TRUE AND (stopped IS NOT TRUE OR stop_confident IS NOT TRUE))",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_review_runs_converged_implies_earned_stop", "review_runs", type_="check"
    )
    op.drop_column("review_runs", "converged")
