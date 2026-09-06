"""A finding has a life between rounds (#772), and a cycle says whether it attested (#782)

Two changes, one file, because they land on one branch and a second revision
would be a second head for `scripts/migration_reconcile.py` to reconcile over a
pair of changes that always ship together. They are separate below and the
downgrade takes them off in reverse.

## 1. `review_finding_ledger` — where a defect stood at the end of a round

A finding exists inside a round and nowhere else. It is raised, judged, maybe
fixed, and what became of it survives as prose in a report plus, if somebody
remembers, one row in `review_finding_outcomes`. Three costs, all measured:

* a finding deferred in round 2 comes back in round 3 looking like fresh damage,
  which lands in `escalate_on.fix_injection` and stops cycles that were
  converging;
* a finding nobody was PAID to look at — a verification budget that ran out — is
  indistinguishable from a finding nobody raised;
* "did round 3's fix answer round 2's complaint" needs a human reading two
  reports.

0 of 27 recorded cycles have ever been marked converged.

### Why a second table and not columns on an existing one

`review_finding_outcomes` is one terminal row per `(repo, pr, finding_key)` —
what a person, or a fixer acting for one, CONCLUDED about a defect. Its unique
constraint is what makes "what happened to this?" a question with one answer, and
its own docstring records why it refuses to be per-round: a defect raised in
rounds 2, 3 and 4 is one refutation and three observations, and a per-round
outcome would multiply one refutation by however long the fix loop ran — largest
exactly where the measurement matters most.

This table has the opposite requirement. Every row is worthless unless it is
stamped with the round it happened in. Adding `round` to the outcome table breaks
the constraint that makes it useful; adding `state` to it lets the last round's
mechanical disposal silently overwrite a decision a human recorded. So: two
tables, two questions, allowed to disagree — the same separation
`review_findings.verdict` and `review_finding_outcomes.outcome` already keep, for
the same reason.

Nor is it columns on `review_findings`. Those rows are immutable statements about
one round ("what a round said is a fact about that round, and later knowledge is
a different fact with a different author"), and a promotion in round 5 of a
finding deferred in round 2 has no round-3 observation to hang on.

### The vocabularies

Named once in `app/finding_lifecycle.py`, which renders the model's CHECKs from
its tuples so they cannot drift the way #578's `needs_human` constraints did.
This file spells them out instead, and that is the rule rather than an exception:
a migration is a frozen artefact and may not import live app code (#344,
`tests/test_migrations_self_contained.py`), so these literals are the vocabulary
AS OF THIS REVISION. A state added later alters the constraint in a migration of
its own, exactly as `mb3e5f721` did for the `chore` class.

* `state` — the five recorded OUTCOMES (`fixed`, `narrowed`, `refuted`,
  `deferred`, `superseded`) EXTENDED with the three an outcome cannot express
  because none of them is a decision: `raised`, `unpaid` (a budget ran out before
  anybody looked), `escalated` (no fix round can settle it). `unpaid` beside
  `deferred` is the point of the whole feature: "nobody looked" and "somebody
  looked and said no" are different facts, only one of them argues for a bigger
  budget, and a single `skipped` bucket cannot tell them apart.
* `source` — which writer: `panel`, `judge`, `budget`, `fix-pass`, `promotion`,
  `human`. mergeCraft's five-writer design (MIT, `findings/ledger.py`) is the
  donor for this column and it is the part most worth taking.

### The two round columns

`round` is when the transition happened. `origin_round` is when the defect first
entered the ledger, carried forward unchanged by every later row — mergeCraft's
"`promote` keeps the original round index", except that its single field has to
choose between the two facts and this keeps both. `origin_round <= round` is a
CHECK, so "how many rounds old is this complaint" is a subtraction rather than a
guess. The API derives it from the earliest stored row and ignores a caller that
sends one: a producer could get it wrong on a retry or a resumed cycle, and the
failure would be silent and flattering — a promoted finding acquiring a fresh
round is exactly the "looks like new damage" defect this table exists to end.

### `UNIQUE (repo, pr, finding_key, round, source)`

One writer's word per finding per round. What it buys is idempotency: the
commonest race here is not two agents, it is one agent whose request the board
accepted and whose client timed out (`qb` gives curl 15 seconds), so the retry
arrives with the row already in. Append-only, that retry doubles a round's ledger
and every count over it.

No second index. The unique constraint's B-tree is also the read path — hydration
selects on `(repo, pr)`, which the leftmost prefix serves — the argument
`review_finding_outcomes` and `review_run_files` both record for theirs.

`ck_review_finding_ledger_repo_canonical` is why that constraint is worth
anything: `Acme/X` and `acme/x` are one repository, and without the fold they are
two ledgers for one defect. Same rule as `ck_review_runs_repo_canonical` (#326,
migration 0033).

### `run_id` is `SET NULL`, never `CASCADE`

Deleting a run must not delete the record of what happened to a defect —
`review_finding_outcomes` refuses to be foreign-keyed to a run at all for that
reason, and this is the weaker form of the same rule: keep the join, refuse the
lifetime.

## 2. `review_runs.attested` (#782)

`converged`'s fifth conjunct, stored beside it. "No finding" has two causes —
nothing was wrong, and nothing ran — and the dry branch reported the first
without being able to tell them apart: a round 1 whose seats produced nothing
anywhere stopped, was confident, had no veto and nothing outstanding, and so read
as a clean converged finish from a panel that never demonstrated it could see the
diff.

Bound rather than listed as dropped, because `GET /review/convergence` is what
this epic is judged on and without the column the board can say a cycle did not
converge and cannot say why. "A P1 was still outstanding" and "the panel raised
nothing at all" are different facts with different repairs, and pooled into one
`unconverged` count the rate says neither.

`ck_review_runs_converged_implies_attested` is its own constraint rather than a
widening of `ck_review_runs_converged_implies_earned_stop`: the two refuse
different things, and a caller is owed the rule that actually refused it.

### Nullable, no server default, no backfill

NULL means the panel did not say — every row in this table today, and every
producer too old to nest the key. **Never `false`**, and the direction matters:
read as a denial it would report the entire archive as having attested to
nothing, and `ck_review_runs_converged_implies_attested` would then have refused
every converged row this board holds. There is no second copy to backfill from; a
round payload lives in a temp directory on whichever host ran the panel.

## Downgrade

`downgrade()` drops the column and the table and **the data is gone**, on
`m7fc78723`'s terms and for its reason. Acceptable for a column and a table no
existing read depends on, and written down here rather than discovered.

Revision ID: m1adf9129
Revises: mb79c396d
Create Date: 2026-09-06 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m1adf9129"
down_revision: str | None = "mb79c396d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The three vocabularies, SPELLED OUT, and the divergence from the model is
#: deliberate rather than an oversight.
#:
#: `app/finding_lifecycle.py` composes the same CHECKs from its tuples, because a
#: constraint written as a literal beside a tuple is how #578's `needs_human`
#: drift happened. A migration is the opposite case: it is a FROZEN ARTEFACT that
#: runs at a fixed point in schema history, so importing the live tuple would make
#: this revision's constraint whatever `app/` says today —
#: `tests/test_migrations_self_contained.py` refuses the import outright and #344
#: has the argument. These literals are the vocabulary AS OF THIS REVISION and are
#: correct forever; a state added later gets a migration of its own that alters
#: the constraint, which is what `mb3e5f721` did for `chore`.
_STATES = ("'deferred', 'escalated', 'fixed', 'narrowed', 'raised', 'refuted', "
           "'superseded', 'unpaid'")
_SOURCES = "'budget', 'fix-pass', 'human', 'judge', 'panel', 'promotion'"
_REASON_REQUIRED = ("'deferred', 'escalated', 'narrowed', 'refuted', "
                    "'superseded', 'unpaid'")

#: The whitespace set every "must actually say something" CHECK in this schema
#: trims by, and the two traps it avoids, restated here because neither is
#: visible in the expression: single-argument `btrim` strips ORDINARY SPACES
#: ONLY (a reason of one tab satisfied it), and vertical tab is spelled `\013`
#: because Postgres' escape strings do not define `\v` — an undefined escape
#: drops the backslash and keeps the character, so `E'\v'` is the LETTER v and
#: the set would have refused a reason of "v" as empty.
_WS = r"E' \t\n\r\f\013'"


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("attested", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "ck_review_runs_converged_implies_attested",
        "review_runs",
        "NOT (converged IS TRUE AND attested IS FALSE)",
    )

    op.create_table(
        "review_finding_ledger",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("pr", sa.Integer(), nullable=False),
        sa.Column("finding_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("origin_round", sa.Integer(), nullable=False),
        sa.Column("cycle", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("revisions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prior_state", sa.Text(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["review_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo", "pr", "finding_key", "round", "source",
                            name="uq_review_finding_ledger_entry"),
        sa.CheckConstraint(f"repo = lower(btrim(repo, {_WS}))",
                           name="ck_review_finding_ledger_repo_canonical"),
        sa.CheckConstraint(f"state IN ({_STATES})",
                           name="ck_review_finding_ledger_state"),
        sa.CheckConstraint(f"source IN ({_SOURCES})",
                           name="ck_review_finding_ledger_source"),
        sa.CheckConstraint(
            f"state NOT IN ({_REASON_REQUIRED}) OR "
            f"(reason IS NOT NULL AND btrim(reason, {_WS}) <> '')",
            name="ck_review_finding_ledger_reason",
        ),
        sa.CheckConstraint(
            f"state <> 'superseded' OR "
            f"(superseded_by IS NOT NULL AND btrim(superseded_by, {_WS}) <> '')",
            name="ck_review_finding_ledger_superseded_by",
        ),
        sa.CheckConstraint("round >= 1", name="ck_review_finding_ledger_round"),
        sa.CheckConstraint("origin_round >= 1 AND origin_round <= round",
                           name="ck_review_finding_ledger_origin_round"),
        sa.CheckConstraint("revisions >= 0", name="ck_review_finding_ledger_revisions"),
    )


def downgrade() -> None:
    op.drop_table("review_finding_ledger")
    op.drop_constraint("ck_review_runs_converged_implies_attested", "review_runs",
                       type_="check")
    op.drop_column("review_runs", "attested")
