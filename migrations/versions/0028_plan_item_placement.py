"""Where an item sits, and who decided that — #183

`POST /plan/item` was documented as safe for agents on a premise stated in one
line: *adding is not reordering, so an agent may do it*. The premise is right and
the implementation broke it, because there was no way to add an item without also
deciding where it went and the endpoint had hard-coded that decision to "last".
Appending is not the absence of an ordering judgement. It is one specific
judgement — "this is the lowest-priority open item" — asserted on the caller's
behalf, every time, and wrong whenever the new item is not in fact the least
important thing outstanding.

The plan was seeded on 2026-08-17 and the gap showed within the minute: told
mid-seed that the appetite gate (#85) was near-top priority, the agent could
append it at rank 20 and nothing else. What it did instead was write the priority
into whatever fields would take a string — a phase reading `"TOP PRIORITY — Rich,
2026-08-17 23:00"` and a note opening `RANK IS WRONG AND A HUMAN MUST FIX IT` —
and `GET /plan` went on answering `next` = rank 1 with no caveat at all.

## Two columns, and neither of them is a rank

`rank_source` says WHO decided this row's position: `appended` (nobody — it went
last because that was the only thing available), `submitted` (it arrived inside a
`POST /plan/submit`, so the submitter chose the order within the batch but not
where the batch sits), `placed` (an agent named a neighbour), `ordered` (a human,
through `POST /plan/reorder`). That is the fact 28 ranked rows could not state:
ranks 1-17 were a real sequence, 18-28 were the order the adds happened to
arrive in, and nothing in the data distinguished them.

`placed_for` is the provenance of a placed row — "Rich, 23:00". It exists so the
sentence that ended up in `phase` has a field of its own; the API refuses it
unless a position is given with it, so it cannot become the second free-text
priority channel that this issue is about.

## Everything already in the table is `appended`, and that is not a guess

The server default is `'appended'`, so every existing row backfills to it — which
is exactly what those rows are. Whatever intent ranks 1-17 encode was never
asserted through an ordering call; it survives only because the adds happened in
the order somebody wanted, which the plan itself calls luck rather than a
mechanism. Marking them `ordered` would invent a human decision that was never
taken, and `GET /plan` reports the ordering as untrusted precisely so a person
can make it once, in the browser, and have the claim become true.

Cheap to apply: two nullable-or-defaulted columns on a table that holds tens of
rows by design, no rewrite of anything else, and no index touched.

Chained after 0027 because a single head is what
`test_the_repos_own_migration_chain_is_single_headed` asserts, and
`scripts/migration_reconcile.py` is what to run if another branch has taken 0028
by the time this lands.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_items",
                  sa.Column("rank_source", sa.Text(), nullable=False,
                            server_default=sa.text("'appended'")))
    op.add_column("plan_items", sa.Column("placed_for", sa.Text(), nullable=True))
    # The vocabulary is enforced rather than agreed, for the same reason
    # `ck_plan_items_state` is: a fifth spelling arriving from one call site is
    # how "stage 1" and "Stage 1" became two phases, and this column is read to
    # decide whether the plan's order can be believed.
    op.create_check_constraint(
        "ck_plan_items_rank_source", "plan_items",
        "rank_source IN ('appended', 'submitted', 'placed', 'ordered')")


def downgrade() -> None:
    # Lossy in one direction only, and the loss is bounded: rolling back drops
    # the record of who chose each position, which is a claim about the rows
    # rather than the rows themselves. Every rank survives untouched, so the plan
    # reads exactly as it did before — it simply stops being able to say how much
    # of that order anybody actually decided.
    op.drop_constraint("ck_plan_items_rank_source", "plan_items", type_="check")
    op.drop_column("plan_items", "placed_for")
    op.drop_column("plan_items", "rank_source")
