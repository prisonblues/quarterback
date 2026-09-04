"""The fix pass is an artifact, and it is not a leaderboard (#624)

`mc57fb9ba` stored the dials a round applied. `m331126c7` stored the working behind
the counts it produced. `mdef4716b` stored the machinery that ran it. Every one of
those describes a ROUND. The actor between two rounds — the fix pass, which writes
the code that produces the next round's findings — had no row of its own.

Everything else in this loop is first-class. `review_reviewers` scores every seat.
`review_findings` records what each round raised, with a key, a provenance bucket
and a recurrence class. `review_finding_outcomes` records what happened to each
defect afterwards, with the identity that said so. The fixer had a paragraph in a
markdown brief.

On `prisonblues/lexray#1780` the four passes came out at +850/-314 across 11 files,
+322/-49 across 9, +356/-41 across 12 (seven of them files no round had read) and
+142/-31 across 7. Nothing anywhere held those as facts about the passes: they were
reconstructed from `git` by hand, weeks later, in order to file the issue that this
revision answers. `fix-and-review.md:72` says outright that the board cannot
reliably tell a fixer's work from a reviewer's.

## Two columns, and the split is the same one #112 made

* `fix_pass` (JSONB, nullable, **deferred** in the model) — the record, verbatim.
  The commit range and which of three readers supplied the diff (`compare`, the
  round's own increment, or #504's local reconstruction); which round's To fix list
  briefed it and how big that list was; the production / test / prose churn split;
  the files it touched and which of them no earlier round of the cycle had read;
  which of the brief's findings this round no longer raises and which it still does;
  how many of this round's own findings were attributed to it; the
  `narrowed`/`declined`/`escalated` keys the pass declared, segregated under
  `declared`; and `gaps`, the record's own account of what it cannot say.

  Deferred because it carries path lists and finding keys, on `rules`' argument: a
  `GET /reviews?limit=500` that fetched it would have Postgres ship five hundred of
  them to the app to serialise none.

* `fix_pass_counts` (JSONB, nullable) — the record's own `counts` block, lifted
  verbatim so it can ride the run LIST. Eleven keys at most, every value a count or
  NULL, which is exactly the trade `provenance_counts` and `unread_files_count`
  already make on this table. #624's instruction is to "calibrate against real
  cycles before anything is scored", and a calibration reads a population: asking
  "how big were the passes on rounds that then attributed nothing to them" must not
  cost one fetch per run.

## Verbatim, and the board derives nothing

Every value in `fix_pass` was derived by the panel from the diff, the commits and
the payload the pass was given — never from the fix pass's account of itself, which
is the constraint #622 and #621 are built on. This board does not recompute one of
them. A second implementation of "how much did this pass churn" would be free to
disagree with the panel about the same pass, which is what `m6bc45ff1` refuses for
`converged` and what #305 was filed over.

The one thing ingest reads is the `counts` sub-object, and it is a LIFT rather than
an interpretation: the keys are checked against a vocabulary the panel publishes,
the values are checked to be counts, and nothing is summed, divided or compared.

## And it is deliberately not a leaderboard

#624's title carries this in its parenthesis, and the constraint shapes the schema
rather than sitting in a comment above it. Its own second opinion supplies the
argument: every obvious ratio over a fix pass is gameable in a direction worse than
the disease.

* lines per finding cleared rewards compressed, superficial, clustered fixes;
* findings introduced per pass rewards weakening tests and avoiding the files most
  likely to be read;
* new files opened rewards refusing a cross-file repair that is genuinely required —
  a P1 left unfixed to protect a metric;
* share of fixes still standing a round later is invalid under increment scope,
  because the later round may never have re-read the repair.

So: **there is no actor column here.** The record names the pass by its commit range
and the round that briefed it, and never the agent, model or session that performed
it — a table with no actor key cannot be aggregated into a ranking of fixers, which
is a stronger guarantee than a policy of not writing the query. There is no ratio
column, no score, no rank. `GET /review/stats`, which is the leaderboard this table
already feeds, does not read either column. `tests/test_review_fix_pass.py` pins all
of that, including a source-level check that nothing in `app/` aggregates or orders
by these columns.

## No index

Every query on this table is already selective on `(repo, pr)` or `ts`, both
indexed. A consumer slicing a population groups on `fix_pass_counts` in the
application after the window has been narrowed — and an index here would be a write
cost paid by every round in order to make the aggregation this feature declines to
offer marginally cheaper. `mdef4716b` made the same call for the harness columns.

## No CHECK

This board has no proposition about these values to enforce. It does not know how
many findings a brief may hold, does not know that `cleared + still_open` should
equal `placed` (the panel guarantees that in-process and a test pins it; a CHECK
would be a second opinion written against today's producer), and `churn: null`
beside a `files: 0` is not a contradiction — it is a fix range that was read and
held no file. A constraint written against today's payload is a 500 waiting for
tomorrow's, which is `mdef4716b`'s own conclusion.

## Nullable, no server default, no backfill

NULL means "there was no pass to record": round 1, a run outside a cycle, and any
round that reviewed nothing. It is also every row in this table today.

**A backfill would be the bug**, on `mdef4716b`'s terms exactly. The inputs a record
needs — the fix range's diff, the anchor round's To fix list, the file set earlier
rounds had in front of them — are read from payload files that were temp files on
whatever host ran the panel, and from a GitHub compare that a rebase can have
invalidated since. Anything reconstructible now would be a guess wearing a
measurement's clothes, and unlike a NULL a wrong churn split cannot be told from a
right one afterwards.

## Downgrade

`downgrade()` drops both and the data is gone, on `m6bc45ff1`'s and `mdef4716b`'s
terms: there is no second copy, the payload it came from is a temp file on another
host, and re-upgrading starts the population again from empty.

Revision ID: mc0d7f83a
Revises: mb41e07c9
Create Date: 2026-09-02 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "mc0d7f83a"
down_revision: str | None = "mb41e07c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs",
                  sa.Column("fix_pass", postgresql.JSONB(astext_type=sa.Text()),
                            nullable=True))
    op.add_column("review_runs",
                  sa.Column("fix_pass_counts", postgresql.JSONB(astext_type=sa.Text()),
                            nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "fix_pass_counts")
    op.drop_column("review_runs", "fix_pass")
