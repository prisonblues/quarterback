"""Which harness produced this round (#112)

`mc57fb9ba` stored the dials a round applied and `m331126c7` stored the working
behind the counts it produced. Both describe how a round was CONFIGURED. Nothing
on the row says what RAN it.

A payload carries `judge_model`, per-reviewer `model`/`effort`/`max_diff_chars`/
`truncated`, `diff_budgets`, `run_key`, `cycle`, `round`, `stop_reason` — a careful
account of the electorate and of the decision — and, until this revision, zero keys
matching `version|harness|commit|digest`. `.harness-rules` argues at length that an
unpinned reviewer MODEL makes "codex found more than claude" unattributable, and
pins three of the four seats for that reason. The harness itself was unpinned and
unrecorded, and it changes far more often than a vendor slug.

## Why now, and why this week in particular

On 2026-08-31 this repository merged six PRs that changed `round_stop`,
`converged`, the `fix_injection` accounting, `restored_lines` and added
`guard_churn`. In the same week the panel on one host was measured twice, two hours
apart, and had been rebuilt underneath a running session in between: one run
without #75's member sandbox and one with it, from the same session, indistinguishable
in the record.

So two rounds of ONE cycle can be read by materially different machinery, and the
r1 -> r2 comparison every stop argument in this system rests on silently assumes
they were not. #637 is about to recalibrate `escalate_on.fix_injection` against a
population of rounds produced this week. A round that runs before these columns
exist has lost its machinery version permanently — the payload is a temp file on
whatever host ran the panel — which is `m6bc45ff1`'s argument for `converged`,
`mc57fb9ba`'s for `review_panel` and `m331126c7`'s for the provenance working,
arriving for the third time with a date on it.

## Four columns, and the split is the whole judgement

The question "what identifies the harness that produced this round" has no single
answer, and `qb-doctor`'s `check_harness` already wrote down why: the truthful
answer lives in the flake pin's rev, which no running harness can reach — it knows
its own store path and nothing about the flake that consumes it — so that tool
compares CONTENT and says in its own docstring that content is a proxy. This
revision records both halves and labels which is which.

* `harness_rev` (Text) — the commit of the checkout the panel ran from.
  **AUTHORITATIVE** where it is not null: it names something a reader can go and
  `git show`. NULL on every INSTALLED harness, which is most of them, because the
  nix store is not a checkout. The panel reports it only when the containing
  repository actually tracks the panel's own file, so a scratchpad copy dropped
  inside some other checkout records NULL rather than that repository's HEAD — a
  plausible 40-hex id in the right column belonging to the wrong repository is the
  one failure worse than an absence.
* `harness_dirty` (Boolean) — whether that checkout's loop directory carried
  changes the rev does not, untracked files included. This is what makes a rev
  honest rather than merely present: `panel-review-pr.md` tells you to run the panel
  from a scratchpad copy, and a dirty copy is the normal case for anybody developing
  it. NULL is "no rev, or nobody could ask", never a silent false.
* `harness_digest` (Text) — a scheme-tagged content hash of the loop modules,
  `loops-sha256-1:<hex>`. A **PROXY**. It cannot name a version and cannot say which
  of two is newer. It is the only field that is always present and never wrong about
  the question an r1 -> r2 comparison actually asks: same code, or not.
* `harness_path` (Text) — where it ran from. A **LOCATOR**, machine-scoped. For a
  nix install it doubles as an exact identity of the build; for a scratchpad copy it
  is the only field that says the round did not come from the deployed harness.

A release NUMBER is not among them, and that is a finding rather than an omission.
`package.nix` ships `loops`, `commands`, `templates`, `claude`, `githooks` and the
harness README into the store — not `pyproject.toml`, not `CHANGELOG.md` — and the
release number is applied on the base after a merge by `scripts/release.py run`, so
a harness built from a branch has no number at all. A version string knowable from
inside a running harness does not currently exist, and one that did would be too
coarse for this week regardless: all six of those merges shipped under one release.

## Verbatim, uninterpreted, unindexed

Stored exactly as sent. This board does not parse a store path, does not recompute
a digest and does not resolve a rev against any repository — it has no checkout of
the harness to resolve one against, and a second implementation of "which harness is
this" is the drift #305 exists to end. What ingest refuses is being storable: a
wrong shape, a blank, a NUL, an over-long value. Refused whole rather than
truncated, on this module's standing rule.

Not indexed. Every query on this table is already selective on `(repo, pr)` or `ts`,
both indexed. A consumer slicing a population groups on these in the application
after the window has been narrowed, and an index that nothing filters on is a write
cost paid by every round.

## Nullable, no server default, no backfill

NULL is "the panel did not say" on all four — every row in this table today, and
permanently for `rev` and `dirty` on every installed harness.

**A backfill would be the bug.** The only value available to write is today's
harness, and attributing today's version to rounds that ran under another is
precisely the mixing this column exists to make visible. It would also be
irreversible in the direction that matters: a NULL can be filled later by nothing
and read honestly forever, and a wrong digest cannot be told from a right one.

## No CHECK

This board has no proposition about these values to enforce. It does not know what a
store path looks like, does not know the digest scheme (the tag is on the value so
it does not have to), and a `harness_dirty: true` beside a null `harness_rev` is not
a contradiction this board can rule out — it is a shape the panel does not currently
send and might, and a constraint written against today's producer is a 500 waiting
for tomorrow's.

## Downgrade

`downgrade()` drops all four and the data is gone, on `m6bc45ff1`'s terms: there is
no second copy, the payload it came from is a temp file on another host, and
re-upgrading starts the population again from empty.

Revision ID: mdef4716b
Revises: m331126c7
Create Date: 2026-09-01 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mdef4716b"
down_revision: str | None = "m331126c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("harness_rev", sa.Text(), nullable=True))
    op.add_column("review_runs", sa.Column("harness_dirty", sa.Boolean(), nullable=True))
    op.add_column("review_runs", sa.Column("harness_digest", sa.Text(), nullable=True))
    op.add_column("review_runs", sa.Column("harness_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_runs", "harness_path")
    op.drop_column("review_runs", "harness_digest")
    op.drop_column("review_runs", "harness_dirty")
    op.drop_column("review_runs", "harness_rev")
