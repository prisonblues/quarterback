"""A blocker is a row: what waits, on whom, for what answer — #328

`depends_on` says *item A waits on item B* and nothing else. It takes plan-item
uuids, so a blocker with no item to point at cannot be written down at all, and
*why*, *who for* and *what question* have nowhere to go.

Measured before this table existed: `counts.blocked` returned **0 across 20 open
items** on a plan where three carried a blocker as English inside `note` — "RANK
IS WRONG AND A HUMAN MUST FIX IT" among them. Countable by nobody, rendered as
ordinary open work, and picked up by `next` like anything else.

The two mechanisms that already looked like they might serve — the `stuck` post
and the `needs-human/*` labels — were both measured empty, and for one reason: an
event is easy to skip and impossible to chase. This is the queue; #274's post
stays the doorbell.

**The class list IS restated here, and that is the rule rather than a lapse.**
The first version of this migration imported `app.needs_human.NEEDS_HUMAN_CLASSES`
on the DRY instinct, and #344's guard caught it: *"a migration is a frozen
artefact; live app code is not."* Had it shipped, adding a seventh class later
would have silently changed what THIS revision means on a fresh replay — invisible
on any database already past it, and detonating on exactly the paths nobody
watches: a new worktree, a disaster-recovery rebuild, `downgrade base && upgrade
head`. So the six values are frozen literals, and
`tests/test_migration_drift.py` is what keeps them honest against the model.

Revision ID: m8f42a01c
Revises: m5b71c2d9
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "m8f42a01c"
down_revision: str | None = "m5b71c2d9"
branch_labels = None
depends_on = None

#: Frozen at this revision. `app.needs_human.NEEDS_HUMAN_CLASSES` is the live
#: definition and these must match it TODAY — the drift test asserts that — but
#: they are copied rather than imported so that widening the vocabulary later
#: cannot reach back and change what this revision did. A seventh class is a new
#: migration, which is also the honest way to record when it was added.
_CLASSES = "'decision', 'taste', 'ui', 'environment', 'auth', 'other'"
_KINDS = "'item', 'issue', 'pr', 'repo'"


def upgrade() -> None:
    op.create_table(
        "blockers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("repo", sa.Text()),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_value", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("owner", sa.Text()),
        sa.Column("raised_by", sa.Text(), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.Text()),
        sa.Column("resolution", sa.Text()),
        sa.CheckConstraint(f"kind IN ({_CLASSES})", name="ck_blockers_kind"),
        sa.CheckConstraint(f"subject_kind IN ({_KINDS})",
                           name="ck_blockers_subject_kind"),
        sa.CheckConstraint("length(question) > 0",
                           name="ck_blockers_question_present"),
        # All three or none. "Resolved, by nobody, saying nothing" is the flag
        # with nothing behind it that #279 made a biconditional to prevent, and
        # the resolution is the payload the next agent actually reads.
        sa.CheckConstraint(
            "(resolved_at IS NULL AND resolved_by IS NULL AND resolution IS NULL)"
            " OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL"
            "     AND resolution IS NOT NULL AND length(resolution) > 0)",
            name="ck_blockers_resolution_complete"),
    )
    op.create_index("ix_blockers_repo", "blockers", ["repo"])
    # PARTIAL, so one open question per (subject, class) while the answered ones
    # accumulate: the resolutions are the record worth keeping, and a unique index
    # over all rows would make answering a question the thing that prevents
    # asking it again later.
    op.create_index("ix_blockers_open_subject", "blockers",
                    ["repo", "subject_kind", "subject_value", "kind"],
                    unique=True, postgresql_where=sa.text("resolved_at IS NULL"))
    op.create_index("ix_blockers_open_owner", "blockers", ["owner"],
                    postgresql_where=sa.text("resolved_at IS NULL"))


def downgrade() -> None:
    # Dropping the table drops the questions AND their answers. Nothing else
    # holds them — that is the whole argument for the row — so a downgrade past
    # this point loses the record rather than degrading it, and there is no
    # honest place to put it: `note` is where these lived before and putting them
    # back would recreate the prose nobody can count.
    op.drop_index("ix_blockers_open_owner", table_name="blockers")
    op.drop_index("ix_blockers_open_subject", table_name="blockers")
    op.drop_index("ix_blockers_repo", table_name="blockers")
    op.drop_table("blockers")
