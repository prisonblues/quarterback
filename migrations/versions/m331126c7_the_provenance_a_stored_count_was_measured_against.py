"""The provenance a stored count was measured against (#647)

`mc57fb9ba` stored `review_panel`, the dial VALUES a round applied, and closed the
fifth instance of one bug: `ReviewIn` is `populate_by_name=True` with no `extra=`,
so pydantic's default `extra="ignore"` applies and a top-level key the panel sends
and that model does not name goes on the floor. `tests/test_payload_key_drift.py`
made the remainder countable — twenty-five keys, dropped on purpose, written out by
hand.

Four of those twenty-five were never "on purpose" in the sense the others are. They
are the WORKING behind `provenance_counts`, a tally this board has stored since
v2.26 and publishes on every view.

## The four, and what each one says about a number already on the row

* `fix_range_source` (#512) — which range answered: `increment` (the diff the round
  reviewed), `compare` (the separate API fetch used under `pr` scope and wherever
  the increment fell back) or `reconstructed` (#504's rebuild after a rewritten
  history). Not one measurement: the increment drops a base-branch merge's files
  and the compare range does not.
* `provenance_restored` (#559) — how much of the fix range the round declined to
  attribute because the cycle had already seen it, which earlier rounds it compared
  against, and `why` where the comparison could not be made at all. This is the
  filter that MOVES `introduced`.
* `rules` (#305) — which layer supplied each dial, with the reason and expiry for a
  board dial. `review_panel` is `panel_seats.Dials.as_dict()`, twelve settings, and
  `escalate_on` is not among them; `rules.dials` carries every dotted path under
  `review_panel.` and `reviewers.` — fifty-two on this repository, and
  `review_panel.escalate_on.fix_injection` is one of them. This is the only field
  on the row that records the threshold a round ran under.
* `scope` / `since_sha` — what the round actually reviewed. `diff_chars` is stored
  and is scope-dependent, so a consumer comparing it across a cycle's rounds
  without these is comparing a whole PR against a commit range.

#642's own changelog states the trap these close: a threshold fitted across rounds
whose `introduced` was measured against a filtered range and rounds where it was
not is a threshold fitted to a denominator that changed underneath it. #637
recalibrates `escalate_on.fix_injection` against exactly those counts.

## Why now

The round payload is written to a temp directory on whatever host ran the panel and
is not kept. A round that runs before these columns exist has lost them
permanently — the same argument `m6bc45ff1` makes for `converged` and `mc57fb9ba`
for `review_panel`, and here it has a date on it: the recalibration cycles this
serves are about to run, and the population they produce is the one #637 has to
work with.

## Five columns and not one

`rules` and `provenance_restored` are objects and get JSONB. `fix_range_source`,
`scope` and `since_sha` are scalars and get their own `Text` columns rather than a
key inside a blob, for two reasons. A consumer recalibrating a threshold reads a
POPULATION and slices it on these, so they ride `_run_view` and every view with it —
the rule `merge_base` and `base_sha` already state, that a scalar whose whole point
is cross-run comparison must not cost one fetch per run. And there is no object on
the payload that holds all five: composing one here would be this board inventing a
structure the panel never sent.

`since_sha` is `Text` beside `head_sha`, `merge_base` and `base_sha`, and goes
through the same coercion: under increment scope the round's target is
`since_sha...head_sha`, so it is the other end of a range whose head end is already
normalised.

## Opaque, unindexed, uninterpreted

The two JSONB columns are stored and never read for meaning. `app/api/dials.py`
makes that case at length, and `rules` sharpens it: interpreting a LAYER would mean
this board learning the resolution order as well as the vocabulary, and a second
implementation of "which file answered" is the drift #305 exists to end.

The three scalars are stored verbatim against no vocabulary either, which is a
decision and not an omission. `pr_state` coerces anything outside `PR_STATES` to
NULL because GitHub's states are a foreign, stable set and a variant spelling
silently reclassifies a PR. Neither half holds here: `fix_range_source` was two
values when #512 published it and #504 added a third, so a frozen set written on
this board would have dropped `reconstructed` on the release that introduced it —
#647's own bug one layer down. And an unrecognised value forms its own group in a
consumer's `GROUP BY` rather than being folded into one it cannot see.

Not indexed. Every query on this table is already selective on `(repo, pr)` or
`ts`, both indexed, and nothing filters on these in SQL.

## Nullable, no server default, no backfill

NULL is "the panel did not say" on all five: every row in this table today, and
permanently for the paths that never establish the value — round 1 attributes
nothing, so its `fix_range_source` is null; round 2's only prior round IS the
anchor, so its `provenance_restored` is null.

A backfill would have to guess. For `rules` the guess would be TODAY's layers
attributed to rounds that ran under whatever was written months ago, which is the
#305 incident manufactured rather than merely unanswerable. For `scope` it would be
inferred from the round number, which is exactly the inference `scope` exists to
stop a reader making — the panel falls back to `pr` whenever the anchor is missing.

## No CHECK

Nothing here is incoherent by construction in a way this board can state. It does
not know what a dial means, it does not enforce the scope vocabulary (see above),
and `scope: "increment"` with a null `since_sha` is a real round — one whose anchor
the panel refused — rather than a contradiction.

## Downgrade

`downgrade()` drops all five and the data is gone, on `m6bc45ff1`'s terms: there is
no second copy, the payload it came from is a temp file on another host, and
re-upgrading starts the population again from empty.

Revision ID: m331126c7
Revises: mc57fb9ba
Create Date: 2026-08-31 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "m331126c7"
down_revision: str | None = "mc57fb9ba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_runs",
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "review_runs",
        sa.Column("provenance_restored",
                  postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("review_runs", sa.Column("fix_range_source", sa.Text(), nullable=True))
    op.add_column("review_runs", sa.Column("scope", sa.Text(), nullable=True))
    op.add_column("review_runs", sa.Column("since_sha", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "since_sha")
    op.drop_column("review_runs", "scope")
    op.drop_column("review_runs", "fix_range_source")
    op.drop_column("review_runs", "provenance_restored")
    op.drop_column("review_runs", "rules")
