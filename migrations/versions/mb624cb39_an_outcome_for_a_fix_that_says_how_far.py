"""A fifth finding outcome, for a fix that says how far it went — #615

The vocabulary was `fixed | refuted | deferred | superseded`, and three of those
four are ways to leave a finding UNFIXED: refute it, defer it, or escalate it
(which lands as a deferral). Every one of them answers *whether* to act. None of
them answered *how far*, and the fixer brief on this repo says "fix everything you
find", "never note a problem and move on", "the standard is not 'good enough' —
it's 'nothing left to improve'". A finding names a symptom at a line; the general
form of that symptom is a class; and with no word for the narrow fix, the maximal
one is the only answer that fully satisfies the brief.

Measured, not supposed — `prisonblues/lexray#1780`, rounds 3-5, three instances in
one cycle:

* One endpoint served 4.65 MB uncompressed because `/api/` had no `gzip` in scope.
  The fix put `gzip` at nginx **server** level, explicitly "so no proxy stanza has
  to be duplicated". The next round's P1 was that it weakened ETags server-wide and
  broke an unrelated endpoint's conditional requests.
* Four server-side readers of a column did not merge a shared glossary. The fix
  wrote a helper and called it from all four; the next round found two more
  readers, as a new P2. The helper CREATED the expectation of exhaustiveness that
  made incompleteness a defect — while the four were the finding, it was not one.
* A merge had been applied to a dict whose docstring says it feeds the renderer.
  The minimal fix was one layer down, at the indexing site, and nothing in the
  vocabulary let the fixer say "the minimal fix is here, the class-wide version is
  a separate change" — so the class-wide version is what got written.

Fixing the class is what makes a pass edit files the finding never named, and those
files are where the next round's findings come from.

`narrowed` is the fifth word (decided by the repo owner, 2026-08-30): **the finding
is real, this pass fixed it at the point it was raised, and the general form is not
this pass's work.**

## It is a FIX, and it sits with `fixed`

Not a flavour of deferral. `app.review_queue.CLEARING_OUTCOMES` clears it and
`round_stop` one directory over counts it answered rather than outstanding, so a
cycle may converge with narrowed findings in it. That is the whole point rather
than a leniency: leave it counting as outstanding and rule 3 holds the cycle open
until somebody writes the class-wide change, which is the behaviour this word
exists to make unnecessary.

## The note is not optional, and it is a second CHECK rather than a wider one

`ck_review_finding_outcomes_narrowed_note` mirrors
`ck_review_finding_outcomes_refuted_note` exactly, with the same two rules that
constraint's own comments spell out — the NOT NULL is not redundant beside the trim
(a CHECK passes when its expression is NULL), and `btrim` gets an explicit
character set because single-argument `btrim` strips ordinary spaces only.

Its own constraint rather than a widened `..._refuted_note`, because the two are
required for different reasons and a caller is owed the one that applies to it: a
violation quoting `refuted_note` at somebody recording a narrowed row names a rule
that does not exist. What the note carries here is the general form — what fixing
the class would have taken — which is the only thing distinguishing this row from
a `fixed` one. A bare `narrowed` is a `fixed` that has lost the word's whole
content, and it would be the cheap exit this change must not create.

## The downgrade refuses once the word is in use — it is one-way in practice

`downgrade()` drops `ck_review_finding_outcomes_narrowed_note`, drops the widened
vocabulary CHECK, and recreates the four-value one. PostgreSQL validates a CHECK
against the existing rows as it adds it, so **that last step raises the moment a
single `narrowed` row exists in `review_finding_outcomes`**, and the whole revision
rolls back with it — Alembic runs it in one transaction (`migrations/env.py`) and
Postgres has transactional DDL, so nothing is left half-applied to clean up.

The refusal is deliberate and is argued in `downgrade()`'s own docstring: the
alternative is quietly rewriting those rows to `fixed`, which destroys the one
distinction the outcome exists to record. The operational consequence is worth saying
out loud, though: **once the first production round has recorded a `narrowed`
finding, this revision can no longer roll the app and the schema back together.**

If a rollback is genuinely wanted, an operator has to clear the word out of the table
first, and decide deliberately what each row becomes:

    SELECT finding_key, note FROM review_finding_outcomes WHERE outcome = 'narrowed';

Each of those is a real fix whose `note` carries the general form nobody wrote.
Rewriting the row to `fixed` loses that sentence's meaning; deleting it loses the
finding's outcome altogether. Either is a judgement about the record, which is exactly
why the migration will not make it on an operator's behalf. Once the query returns no
rows, `alembic downgrade` succeeds unchanged — there is nothing else to undo.

## The vocabulary is frozen here on purpose

Five values spelled out rather than imported from `app.api.reviews`. #344's rule,
which this table's creation migration (0020) states at length: a migration is a
frozen artefact and live app code is not. `tests/test_migration_drift.py` is what
proves the two agree today, and `app/api/reviews.py` fails at import if its
`OUTCOMES` tuple and the CHECK on the model disagree.

Revision ID: mb624cb39
Revises: mb3e5f721
Create Date: 2026-08-30
"""

from __future__ import annotations

from alembic import op

revision: str = "mb624cb39"
down_revision: str | None = "mb3e5f721"
branch_labels = None
depends_on = None

_TABLE = "review_finding_outcomes"
_VOCABULARY = "ck_review_finding_outcomes_vocabulary"
_NARROWED_NOTE = "ck_review_finding_outcomes_narrowed_note"

#: Frozen at this revision. `app.api.reviews.OUTCOMES` is the live definition.
_AFTER = ("fixed", "narrowed", "refuted", "deferred", "superseded")
_BEFORE = ("fixed", "refuted", "deferred", "superseded")

#: The evidence rule, character for character as `ck_review_finding_outcomes_refuted_note`
#: writes it. Vertical tab is `\013` and NOT `\v`: Postgres' escape strings do not define
#: `\v`, and an undefined escape drops the backslash and keeps the character, so `E'\v'`
#: is the letter v — which would trim v's off both ends of a note and refuse "v" as empty.
_NOTE_REQUIRED = (
    r"outcome <> 'narrowed' OR (note IS NOT NULL "
    r"AND btrim(note, E' \t\n\r\f\013') <> '')"
)


def _vocabulary(values: tuple[str, ...]) -> str:
    return "outcome IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint(_VOCABULARY, _TABLE, type_="check")
    op.create_check_constraint(_VOCABULARY, _TABLE, _vocabulary(_AFTER))
    op.create_check_constraint(_NARROWED_NOTE, _TABLE, _NOTE_REQUIRED)


def downgrade() -> None:
    """Narrow the vocabulary back, and FAIL if a row is already using the word.

    PostgreSQL validates a CHECK against existing rows when it is added, so a
    downgrade over a table that has since stored a `narrowed` raises rather than
    succeeding — and that is the correct outcome, not a rough edge. The same
    argument `mb3e5f721` makes for `chore`, and it is sharper here: the
    alternative is a downgrade quietly rewriting those rows to `fixed`, which
    would destroy the one distinction the outcome exists to record, on the path
    nobody watches, and inflate `precision_after` on the way past.
    """
    op.drop_constraint(_NARROWED_NOTE, _TABLE, type_="check")
    op.drop_constraint(_VOCABULARY, _TABLE, type_="check")
    op.create_check_constraint(_VOCABULARY, _TABLE, _vocabulary(_BEFORE))
