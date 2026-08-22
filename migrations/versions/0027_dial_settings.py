"""Board-sourced harness dials — #305

`review_panel.fix_severity_floor` decides which findings a fix pass may touch and
`round_trigger_floor` decides which ones buy another round. Between them they
decide what a review costs and what it is worth, and changing either was a commit
on a pull request — reviewed by the panel those very dials configure. This table
is the third layer that makes a dial a SETTING: the repo supplies a default, the
board states the value in force, and the resolver names which one answered.

## Nothing to backfill, and refusing to invent one is the point

Every dial in the fleet currently has an answer — DEFAULTS, or a repo's
`.harness-rules.sample` — and that answer stays exactly where it is. Seeding this
table from those files would create the second source of truth the whole design
exists to refuse: two places stating a floor, disagreeing the first time somebody
edits one, and no way to tell which ran. The table starts EMPTY, and an empty
table means every repo resolves precisely as it does today, byte for byte.

## `value` is wrapped, and the wrapper is the migration's one subtlety

`value jsonb NOT NULL` holds `{"value": <anything>}` rather than the bare value.
`null` is the documented off switch for `max_fix_growth`, `distant_merge_lines`
and `escalate_on.premise_repeated`, and a bare JSONB column cannot tell the JSON
value `null` from SQL NULL once an ORM has serialised Python `None` into it. The
wrapper makes "set it to null" and "there is no row" two different facts, which
is what they are.

## `ix_dial_settings_live` coalesces the scope

UNIQUE on `(COALESCE(repo, ''), dial)` over rows that have not been cleared. The
COALESCE is not tidiness: a UNIQUE index treats two NULLs as distinct, so the
FLEET scope — the one #276's throttle writes to — would have been the single
scope able to hold two contradictory live rows for one dial, resolved by whichever
the planner returned first.

Partial over `cleared_at IS NULL` rather than over "not expired", because `now()`
is not immutable and Postgres will not index on it. An expired row therefore still
occupies the slot; a write clears whatever is there, expired or not, and inserts
beside it, which keeps the history of who moved a floor and what they said about
it.

Chained after 0026 because a single head is what
`test_the_repos_own_migration_chain_is_single_headed` asserts, and
`scripts/migration_reconcile.py` is what to run if another branch has taken 0027
by the time this lands.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dial_settings",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("dial", sa.Text(), nullable=False),
        sa.Column("value", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_by", sa.Text(), nullable=True),
        sa.CheckConstraint("length(btrim(dial)) > 0", name="ck_dial_settings_dial"),
        sa.CheckConstraint("length(dial) <= 200", name="ck_dial_settings_dial_len"),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_dial_settings_reason"),
        sa.CheckConstraint("repo IS NULL OR length(btrim(repo)) > 0",
                           name="ck_dial_settings_repo"),
    )
    op.create_index("ix_dial_settings_live", "dial_settings",
                    [sa.text("COALESCE(repo, '')"), "dial"], unique=True,
                    postgresql_where=sa.text("cleared_at IS NULL"))
    op.create_index("ix_dial_settings_scope", "dial_settings", ["repo", "dial"])


def downgrade() -> None:
    # Wholly reversible, and lossless in the only sense that matters: the table is
    # new, holds nothing derived from anywhere else, and nothing outside it was
    # changed to make room. Rolling it back returns every repo to resolving from
    # its own committed default, which is what it did the day before this landed
    # and is the behaviour an empty table already produces.
    op.drop_index("ix_dial_settings_scope", table_name="dial_settings")
    op.drop_index("ix_dial_settings_live", table_name="dial_settings")
    op.drop_table("dial_settings")
