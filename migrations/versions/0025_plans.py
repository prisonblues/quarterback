"""A plan is a row — #172

`plan_items.phase` was a free-text string on an item. It was the plan's own
instance of the defect #172 is about: **a name composed by whoever typed it**.

Four consequences, all observed:

  * "stage 1" and "Stage 1" were two phases, and nothing could tell.
  * A phase had no state, so nothing could say it was finished — only its items
    could, one at a time.
  * A phase could not be CLAIMED. #172's one genuinely fuzzy race is two agents
    surveying the same vague problem at once, before any item exists to claim.
    There was no object at that grain to hold.
  * A plan arrived one `POST /plan/item` at a time, so an eight-item plan landed
    incrementally and a second agent could raid a half-written one — the same
    race moved earlier and made worse.

A row fixes all four. `ix_plans_open_label` makes "one open plan per label per
scope" a database fact; `state` lets a plan finish; the id gives it a derived
claim key (`work`/`plan:<uuid>`) through the one claim table; and
`POST /plan/submit` writes the plan and every item in a single transaction.

## The data migration

Every distinct (repo, phase) becomes one open plan, and the items that named it
point at it. `added_by` is taken from the item that carried the phase and ranks
first — the plan's author is whoever started the phase, which is the truest thing
this schema can know about a string.

Case is folded when GROUPING, because that is the whole point: "stage 1" and
"Stage 1" collapse into one plan rather than two. The label KEPT is the
first-ranked item's spelling, not a lower-cased one — a plan's label is read by
people, and `min()` over the group would have picked by byte order rather than by
who named it.

## Reversible, and lossy in one direction only

`downgrade` writes each plan's label back into `phase` on its items, so the
string survives a rollback. What does not survive is a plan's own note, state and
identity — there is nowhere on an item to put them. That is stated rather than
hidden: rolling this back after claiming plans loses the claims' referent, and the
claims themselves lapse on their TTL.

Chained after 0024 because a single head is what
`test_the_repos_own_migration_chain_is_single_headed` asserts.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by", sa.Text(), nullable=True),
        sa.CheckConstraint("state IN ('open', 'done', 'dropped')", name="ck_plans_state"),
        sa.CheckConstraint("length(btrim(label)) > 0", name="ck_plans_label"),
    )
    op.create_index("ix_plans_open_label", "plans",
                    [sa.text("COALESCE(repo, '')"), sa.text("lower(label)")],
                    unique=True, postgresql_where=sa.text("state = 'open'"))
    op.create_index("ix_plans_repo_state", "plans", ["repo", "state"])

    op.add_column("plan_items", sa.Column("plan_id", sa.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_plan_items_plan_id", "plan_items", ["plan_id"])
    op.create_foreign_key("fk_plan_items_plan", "plan_items", "plans",
                          ["plan_id"], ["id"], ondelete="RESTRICT")

    # One plan per (scope, folded label). DISTINCT ON picks the row the ORDER BY
    # puts first inside each group, which is how the first-ranked item's own
    # spelling and author reach the plan rather than an alphabetical accident.
    op.execute("""
        INSERT INTO plans (repo, label, added_by, state, created_at, updated_at)
        SELECT DISTINCT ON (COALESCE(repo, ''), lower(btrim(phase)))
               repo, btrim(phase), added_by, 'open', created_at, now()
          FROM plan_items
         WHERE phase IS NOT NULL AND btrim(phase) <> ''
         ORDER BY COALESCE(repo, ''), lower(btrim(phase)), rank, created_at
    """)
    op.execute("""
        UPDATE plan_items i
           SET plan_id = p.id
          FROM plans p
         WHERE i.phase IS NOT NULL
           AND btrim(i.phase) <> ''
           AND COALESCE(i.repo, '') = COALESCE(p.repo, '')
           AND lower(btrim(i.phase)) = lower(p.label)
    """)
    op.drop_column("plan_items", "phase")


def downgrade() -> None:
    op.add_column("plan_items", sa.Column("phase", sa.Text(), nullable=True))
    op.execute("""
        UPDATE plan_items i
           SET phase = p.label
          FROM plans p
         WHERE i.plan_id = p.id
    """)
    op.drop_constraint("fk_plan_items_plan", "plan_items", type_="foreignkey")
    op.drop_index("ix_plan_items_plan_id", table_name="plan_items")
    op.drop_column("plan_items", "plan_id")
    op.drop_index("ix_plans_repo_state", table_name="plans")
    op.drop_index("ix_plans_open_label", table_name="plans")
    op.drop_table("plans")
