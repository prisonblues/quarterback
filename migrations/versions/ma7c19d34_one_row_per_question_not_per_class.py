"""A blocker row is one QUESTION, not one class per subject — #576

#328 gave the fleet a row for *a human has to answer this* and a partial unique
index to keep a loop from filling it. The index was `(repo, subject_kind,
subject_value, kind)`, and `kind` is the needs-human CLASS — six words, of which
`environment` carries nearly all the machine-shaped escalations. So a producer
asking several different things about one repo got one row.

Measured on the live board the day this was written. `qb-doctor` had raised
`landed` (*"4 pull requests ready and the tip of main has not moved"*), `harness`
(*"13 scripts differ from this checkout"*) and `unpushed` (*"25 commits on 11
branches exist on no remote"*) against `prisonblues/quarterback`. Two of the three
were answered `"an open blocker already asks this of this subject"` and thrown
away; the table held **one** row while five distinct questions were outstanding.
The `stuck` posts were all correct and all present — #569 made them per-condition
that same day — so the doorbell rang for every one of them and the queue behind it
undercounted by three. That is #274's deduplication protecting the fleet from
noise by hiding the news, arrived at one layer lower.

`condition` is the third part of the key: WHICH standing question. The boundary it
draws lives in `app/models/blocker.py` and is not restated here beyond the one
line that matters — **a condition names the fault, never the reading** — because a
key wide enough to admit the reading (`landed-4-prs`) fills the table just as
surely from the other direction.

## Two other things this revision settles

**`NULLS NOT DISTINCT`.** `repo` is nullable and NULL means fleet scope — a real
value, not a missing one. Under PostgreSQL's default a NULL never equals a NULL in
a unique index, so this index has never deduplicated fleet-scope rows at all, and
`raise_blocker`'s `repo IS NULL` recovery branch could not run for want of a
collision to recover from. An idempotency promise that holds for repo-scoped rows
and quietly does not for fleet-scoped ones is worse than none, because the
docstring is read as covering both. PostgreSQL 15 is the floor here (docker-compose
and CI both pin `postgres:15-alpine`) and 15 is where `NULLS NOT DISTINCT` arrived.

**The length bound is a table invariant, not an API preference.** 120 characters,
frozen as a literal rather than imported from `MAX_CONDITION` — #344's rule, which
this table's own creation migration states at length: a migration is a frozen
artefact and live app code is not.

Revision ID: ma7c19d34
Revises: m8f42a01c
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "ma7c19d34"
down_revision: str | None = "m8f42a01c"
branch_labels = None
depends_on = None

#: Frozen at this revision; `app.models.blocker.MAX_CONDITION` is the live
#: definition and `tests/test_migration_drift.py` is what proves they agree today.
_MAX_CONDITION = 120

_INDEX = "ix_blockers_open_subject"
_OPEN = "resolved_at IS NULL"


def upgrade() -> None:
    # NOT NULL with a server default, so every row that exists becomes `''` —
    # "the subject and the class are the whole question", which is exactly what
    # those rows meant before this column existed. Nullable would have been the
    # softer choice and the wrong one: NULLs are distinct in a unique index, so a
    # nullable `condition` would switch deduplication off for every producer that
    # passes nothing, which is most of them.
    op.add_column("blockers",
                  sa.Column("condition", sa.Text(), nullable=False,
                            server_default=sa.text("''")))
    op.create_check_constraint("ck_blockers_condition_length", "blockers",
                               f"length(condition) <= {_MAX_CONDITION}")
    # Dropped and rebuilt rather than built alongside. Ordinary DDL in the
    # migration's own transaction: this table held five rows fleet-wide when the
    # revision was written and the board is a single container, so
    # `CREATE UNIQUE INDEX CONCURRENTLY` — which cannot run inside a transaction,
    # can leave an invalid index behind, and exists for tables where the lock is
    # measured in minutes — would buy nothing and cost a recovery path.
    op.drop_index(_INDEX, table_name="blockers")
    op.create_index(_INDEX, "blockers",
                    ["repo", "subject_kind", "subject_value", "kind", "condition"],
                    unique=True, postgresql_where=sa.text(_OPEN),
                    postgresql_nulls_not_distinct=True)


def downgrade() -> None:
    """Narrow the key back, and FAIL if that would lose a question.

    Recreating the four-part index over a table that has since accumulated two
    open conditions on one subject raises a unique violation, and that is the
    correct outcome rather than a rough edge. The alternative is for a downgrade
    to pick one unanswered question per subject and delete the rest to make the
    index fit — silently, on the path nobody watches. #328's argument for the
    table is that these were previously prose that nobody could count; a
    downgrade that quietly drops some of them is that failure with a schema.
    """
    op.drop_index(_INDEX, table_name="blockers")
    op.create_index(_INDEX, "blockers",
                    ["repo", "subject_kind", "subject_value", "kind"],
                    unique=True, postgresql_where=sa.text(_OPEN))
    op.drop_constraint("ck_blockers_condition_length", "blockers", type_="check")
    op.drop_column("blockers", "condition")
