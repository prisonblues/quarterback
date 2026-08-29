"""A seventh needs-human class, for when NO judgement is owed — #578

#279's vocabulary has six classes and every one of them names a kind of judgement
a person owes: `decision`, `taste`, `ui`, `environment`, `auth`, `other`. There
was no word for the case where nobody has to judge anything — where the fleet can
state the remedy exactly and merely is not permitted to perform it.

Measured on this board before the change: ten `stuck` posts over two days, from
two machines, every single one classed `environment`. One of them read *"4 pull
requests (#566, #565, #564, #538) ready to land and the tip of main was committed
2h 21m ago"*. Nothing about that was unclear, nobody had to weigh anything, and
when a person finally reached it the whole remedy was `gh pr merge` four times.
It was filed under `environment` because that was the least-wrong of six wrong
options and `other` would have said even less.

`chore` is the seventh. It asserts a property of the ITEM — *this contains no
judgement* — and not a course of action.

## What this migration does NOT do

**It grants nothing permission to act.** Widening a CHECK lets a row SAY "no
judgement is owed"; nothing in this repository reads the class and merges, lands,
deploys or retries on the strength of it, and that separation is the point rather
than an omission. #85, #86, #78 and #335 each settled the same argument from a
different direction: the party that classifies must not also be the party that
acts on the classification.

## Three constraints, not one

The issue names `ck_blockers_kind` and it is the only one built from the live
tuple (`app/models/blocker.py`). The other two were written as literal
six-element lists and had to be found by reading:
`ck_review_findings_needs_human_class` and
`ck_review_finding_reports_needs_human_class`. Widening only `blockers` would
have left a review finding unable to carry the seventh class, and the symptom
would have been an ingest failing on the finding path rather than anything
visible at the vocabulary. Both are now composed from `NEEDS_HUMAN_CLASSES` in
the model, so the next word costs one migration and no archaeology.

## The list is frozen here on purpose

Seven values spelled out rather than imported from `app.needs_human`. #344's
rule, which this table's own creation migration states at length: a migration is
a frozen artefact and live app code is not. `tests/test_migration_drift.py` is
what proves the two agree today.

Revision ID: mb3e5f721
Revises: ma7c19d34
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op

revision: str = "mb3e5f721"
down_revision: str | None = "ma7c19d34"
branch_labels = None
depends_on = None

#: Frozen at this revision. `app.needs_human.NEEDS_HUMAN_CLASSES` is the live
#: definition; `tests/test_migration_drift.py` replays this file and compares.
_CLASSES = ("decision", "taste", "ui", "environment", "auth", "chore", "other")
_BEFORE = ("decision", "taste", "ui", "environment", "auth", "other")


def _sql_list(classes: tuple[str, ...]) -> str:
    return ", ".join(f"'{c}'" for c in classes)


#: (table, constraint, column, nullable) — the three places the closed vocabulary
#: is enforced. `blockers.kind` is NOT NULL and the two review columns are not,
#: so their predicates differ and are written out rather than generated from a
#: flag that a reader would have to hold in their head.
_BLOCKERS = ("blockers", "ck_blockers_kind")
_FINDINGS = ("review_findings", "ck_review_findings_needs_human_class")
_REPORTS = ("review_finding_reports", "ck_review_finding_reports_needs_human_class")


def _apply(classes: tuple[str, ...]) -> None:
    values = _sql_list(classes)
    for table, name in (_BLOCKERS, _FINDINGS, _REPORTS):
        op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(_BLOCKERS[1], _BLOCKERS[0], f"kind IN ({values})")
    for table, name in (_FINDINGS, _REPORTS):
        op.create_check_constraint(
            name, table,
            f"needs_human_class IS NULL OR needs_human_class IN ({values})")


def upgrade() -> None:
    _apply(_CLASSES)


def downgrade() -> None:
    """Narrow the vocabulary back, and FAIL if a row is already using the word.

    PostgreSQL validates a CHECK against existing rows when it is added, so a
    downgrade over a table that has since stored a `chore` raises rather than
    succeeding — and that is the correct outcome, not a rough edge. The
    alternative is for a downgrade to rewrite those rows to `environment` or
    `other` on the path nobody watches, which would put back exactly the
    misclassification #578 was filed to remove, silently, and lose the evidence
    that the class was being used at all.
    """
    _apply(_BEFORE)
