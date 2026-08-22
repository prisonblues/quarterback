""""A human has to look at this" becomes a stored, countable fact — #279

The harness forms this judgement in four places and none of them can record it:
``epic.py`` prints its not-agent-doable ruling, ``panel-review-pr`` step 3a
leaves a ``deferred`` outcome, ``preland`` leaves an exit code, a panel seat
leaves prose in a JSONB list. Four vocabularies, none shared, none countable —
and the ``needs-decision`` label #63's watcher reads has never existed in this
repo. This migration is the storage half: the vocabulary lives in
``app/needs_human.py`` and the labels are created by
``scripts/needs_human_labels.py``.

## Nothing to backfill, and there is nothing that could be

Every existing row lands on ``needs_human = false`` with a NULL class and a NULL
reason, which is exactly what it means: nobody was ever asked. There is no prose
anywhere to mine — the four producers above discarded the judgement rather than
storing it somewhere awkward — so a backfill would be an invention, and it would
be indistinguishable afterwards from a real declaration.

## Why not a column on ``could_not_assess``

``panel_seats.py``'s own measurement: on PR #160 round 1, nine
``could_not_assess`` declarations asked about a file in this repo — 47% of that
round's veto lines — and all nine were answered with ``grep`` in about four
minutes. That field means "I lacked context", a gap a tool or a wider scope
closes. This one means "no context would close this". One column holding both
would put a grep-able question and a design decision in the same bucket, and the
whole design of ``could_not_assess`` is that two states which look alike must not
collapse.

## The evidence CHECK is a biconditional, and both halves are load-bearing

``ck_*_needs_human_evidence`` refuses a flag with no class and no non-blank
reason, AND refuses a class or reason with no flag.

The forward half is #77's rule arriving one level up: a bare flag is a confident
assertion with nothing behind it, and this one ENDS a fix cycle rather than
merely disagreeing with a judge. It has to hold at the boundary, not only in the
API, or a backfill or an admin script inserts one by another door.

The backward half stops orphan evidence: a class and a reason sitting on an
unflagged row read exactly like a declaration somebody later withdrew, and
nothing in the table could tell those apart.

Two spellings inside it are traps this repo has already paid for once, on
``ck_review_finding_outcomes_refuted_note``. ``btrim`` is given an explicit
character set because single-argument ``btrim`` strips ordinary spaces only — a
reason of one tab satisfied it. And vertical tab is ``\\013`` and not ``\\v``:
Postgres' escape strings do not define ``\\v``, and an undefined escape drops the
backslash and keeps the character, so ``E'\\v'`` is the letter v — the set would
have trimmed v's off both ends and refused a reason of "v" as empty.

## ``ix_review_findings_needs_human`` is partial

``GET /review/needs-human`` reads the flagged rows and nothing else, so the index
covers exactly those and carries the class beside them. A flagged finding is meant
to be a small minority of a large table; a full index would be paid for on every
ingest to serve a query that only ever wants the few.

Chained after 0028 because a single head is what
``test_the_repos_own_migration_chain_is_single_headed`` asserts, and
``scripts/migration_reconcile.py`` is what to run if another branch has taken
0029 by the time this lands — which is exactly what happened to this file's first
cut: it was written as 0028 and #282's order-proposal ledger landed on that
number first.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels = None
depends_on = None

#: A frozen snapshot of ``app.needs_human.NEEDS_HUMAN_CLASSES``, deliberately not
#: an import of it. A migration that imported a live constant would replay
#: differently after the constant moved, which is the one thing a migration may
#: not do. ``app/api/reviews.py`` carries an import-time guard that compares the
#: live tuple against the model's CHECK, so the two that CAN be compared are.
_CLASSES = "'decision', 'taste', 'ui', 'environment', 'auth', 'other'"

#: Both halves of the evidence rule, as one expression — see the module docstring.
_EVIDENCE = (
    r"(needs_human AND needs_human_class IS NOT NULL "
    r"AND needs_human_reason IS NOT NULL "
    r"AND btrim(needs_human_reason, E' \t\n\r\f\013') <> '') "
    r"OR (NOT needs_human AND needs_human_class IS NULL "
    r"AND needs_human_reason IS NULL)"
)

_FLAGGED = (
    ("review_findings", "ck_review_findings"),
    ("review_finding_reports", "ck_review_finding_reports"),
)


def upgrade() -> None:
    for table, prefix in _FLAGGED:
        op.add_column(table, sa.Column("needs_human", sa.Boolean(), nullable=False,
                                       server_default=sa.text("false")))
        op.add_column(table, sa.Column("needs_human_class", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("needs_human_reason", sa.Text(), nullable=True))
        op.create_check_constraint(
            f"{prefix}_needs_human_class", table,
            sa.text(f"needs_human_class IS NULL OR needs_human_class IN ({_CLASSES})"),
        )
        op.create_check_constraint(f"{prefix}_needs_human_evidence", table, sa.text(_EVIDENCE))

    op.create_index("ix_review_findings_needs_human", "review_findings",
                    ["needs_human_class"], postgresql_where=sa.text("needs_human"))

    # The per-seat count, tallied server-side from the findings at ingest like
    # every sibling counter on this table, so a scorecard cannot contradict the
    # findings it summarises. NOT NULL with a 0 default: pre-#279 rows read as a
    # panel that never flagged, which is true — nobody could.
    op.add_column("review_reviewers", sa.Column("human_flagged", sa.Integer(), nullable=False,
                                                server_default=sa.text("0")))
    op.create_check_constraint("ck_review_reviewers_human_flagged_non_negative",
                               "review_reviewers", sa.text("human_flagged >= 0"))


def downgrade() -> None:
    # Lossy, and the loss is the whole feature: every declaration recorded while
    # this was live is gone, because there is nowhere else it was kept. Said
    # plainly rather than left to be discovered — the alternative reading, that a
    # rollback is free, is what makes somebody run one.
    op.drop_constraint("ck_review_reviewers_human_flagged_non_negative", "review_reviewers",
                       type_="check")
    op.drop_column("review_reviewers", "human_flagged")
    op.drop_index("ix_review_findings_needs_human", table_name="review_findings")
    for table, prefix in _FLAGGED:
        op.drop_constraint(f"{prefix}_needs_human_evidence", table, type_="check")
        op.drop_constraint(f"{prefix}_needs_human_class", table, type_="check")
        op.drop_column(table, "needs_human_reason")
        op.drop_column(table, "needs_human_class")
        op.drop_column(table, "needs_human")
