"""v2.37: what happened to a finding AFTER the judge ruled on it

`review_findings.verdict` is set once, at review time, by a judge that has no
more access to the answer than the reviewer that raised the finding — and then
`/review/stats` ranks reviewers on it. The feedback loop closes before anybody
has tried to act on the finding, so the one signal that would separate a good
reviewer from a plausible-sounding one is never collected.

**Measured, not supposed.** On PR #64 three of six judge-confirmed P2s were
wrong: `package.nix`'s `installPhase` does `install -m 0755 bin/*` and globs;
`CLAUDE_CODE_SESSION_ID` is exported by every session in this repo; `sed -n
'4,34p'` already ends on the last help line and the "fix" would have printed the
COLORS section into `--help`. All three were conditionals from a reviewer that
had declared it could not assess the condition, in a round that was a panel of
one (#68), and the judge confirmed them because they are well argued. They sit
in the board today as confirmed findings, indistinguishable from the real ones.
The opposite case is on record too — #32 r2's `output_tokens_details.thinking_
tokens` "is not a shape Claude's usage object has", refuted by a transcript on
this box carrying it in all 801 assistant usage blocks, and recorded nowhere.

## Why a table and not a column

An outcome is per DEFECT: one row per (repo, pr, finding_key). A defect raised
in rounds 2, 3 and 4 of a cycle is three `review_findings` rows and one thing
that happened to it. A column on the finding would fan one refutation out across
however many rounds happened to raise it — and the round count correlates with
precisely the long fix loops this measure exists to judge, so the error would be
largest where the number matters most.

It also keeps the finding rows immutable. What a round said is a fact about that
round; what somebody found out afterwards is a different fact with a different
author, a different timestamp and its own attestation. Merging the two would mean
a round's record changes after the fact, which is the property that makes
`GET /review/findings` chains readable at all.

## The columns that are not the outcome

* `note` — the API requires one for `refuted`. A bare `refuted` flag is a
  confident assertion with nothing behind it, which is the exact failure this
  release exists to measure, arriving one level up.
* `deferred_to` and `superseded_by` are separate columns rather than one `ref`.
  Two readings of one field is how a consumer ends up guessing which it has.
* `set_by` / `attested_by` — who recorded it, and the human who signed it off.
  #77 is explicit that an agent must not mark its own findings `refuted`
  unattended; the API cannot tell a fixer from a reviewer, so the record carries
  who said it and the stats publish the attested split rather than pretending a
  guard was enforced. NULL `attested_by` is *unattended*, reported and not
  refused: refusing it would leave the refutation exactly where it is today, in
  prose that nothing counts.
* `revisions` / `prior_outcome` — a terminal state that moves is legitimate (a
  deferred finding is later fixed) and a silent flip is not. Together they say an
  answer changed and what it was, so a window whose after-the-fact precision
  improved can be told from one whose refutations were quietly rewritten.

## Constraints

The vocabulary is a CHECK as well as an ingest validation, because this table
feeds a published precision figure: an unknown value would drop out of the
numerator while still counting as coverage, which reads as "recorded, and it was
neither fixed nor refuted" — the most flattering possible way to say nothing.
The same argument puts the evidence rule at the boundary, so a backfill or an
admin script cannot insert a bare contradiction of the judge either. Both halves
of it are needed: `btrim(note) <> ''` refuses a whitespace note, and the
`note IS NOT NULL` beside it is not redundant — **a CHECK passes when its
expression evaluates to NULL**, so the trim test alone would let a null note
straight through, which is the row the rule exists to refuse.

**The vocabulary is spelled out here rather than imported from
`app.api.reviews.OUTCOMES`, deliberately.** A migration is a snapshot of what the
schema became on a particular day; one that imported a live constant would replay
differently after the constant moved, which is the single thing a migration may
not do. The two definitions that *can* drift within one deployment — the constant
and the model's CHECK — are compared at import instead, and the mismatch is named.

The unique constraint is the read path too. `(repo, pr, finding_key)` is what the
stats join matches on and its leftmost prefix serves the by-PR chain lookup, so
there is deliberately no second index — the same argument `review_run_files`
records for its own.

The revision number and the release number are unrelated counters: this is schema
revision **0020** and it ships in product version **v2.37**.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_finding_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("pr", sa.Integer(), nullable=False),
        sa.Column("finding_key", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("deferred_to", sa.Text()),
        sa.Column("superseded_by", sa.Text()),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("session", sa.Text()),
        sa.Column("attested_by", sa.Text()),
        sa.Column("revisions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prior_outcome", sa.Text()),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("repo", "pr", "finding_key", name="uq_review_finding_outcome"),
        sa.CheckConstraint(
            "outcome IN ('fixed', 'refuted', 'deferred', 'superseded')",
            name="ck_review_finding_outcomes_vocabulary",
        ),
        sa.CheckConstraint(
            "outcome <> 'refuted' OR (note IS NOT NULL AND btrim(note) <> '')",
            name="ck_review_finding_outcomes_refuted_note",
        ),
        sa.CheckConstraint("revisions >= 0", name="ck_review_finding_outcomes_revisions"),
    )


def downgrade() -> None:
    op.drop_table("review_finding_outcomes")
