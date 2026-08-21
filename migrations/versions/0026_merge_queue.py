"""The landing queue — #227

`kind='merge'` said *somebody is landing on this branch right now* and could not
say who was next. So every review-clean PR behaved as though it were, and five of
them each merged the base, pushed, waited for CI, re-ran preland and discovered
somebody else had landed — #80's quadratic integration cost, plus each loser's
push invalidating the winners' green checks on the way past.

This table is the order, and it is deliberately additive: nothing that already
exists is altered, moved or backfilled, and no row anywhere else acquires a new
meaning. The `kind='merge'` claim in `resource_leases` is untouched and still the
only thing that says a land is in progress; `merge_queue_entries` is ordering and
visibility *around* it, never a second lock.

## Nothing to backfill, and that is the point

A queue entry is a live assertion about a pull request — its head commit, a
preland verdict about that commit, and a lease that expires. None of the three
can be reconstructed from history, so there is no honest backfill: inventing
entries for the open PRs would put every one of them in a line at a commit nobody
checked, with a `ready` verdict nobody gave. The queue starts empty and fills as
agents enqueue, which costs one poll each and asserts nothing false.

## The two indexes carry the two guarantees

`ix_merge_queue_open` is UNIQUE on `(repo, base, pr)` over entries that have not
left, so idempotency is a database fact: however many agents or retries call
`POST /merge-queue/enqueue` for one PR, there is one place in the line. Partial
over `left_at IS NULL` for the same reason `ix_resource_leases_held` is partial
over `released_at IS NULL` — history accumulates (who was queued behind whom, and
whether they landed or lapsed, is worth asking after the fact) while at most one
entry per PR can be outstanding.

`ix_merge_queue_order` is the read: one queue, oldest arrival first, with `pr`
breaking a tie on `entered_at`. Two entries can share a timestamp, and an order
that then depended on which row the planner returned first would report two
different heads on two consecutive reads — worse than no queue, because both
agents would believe they were next.

## `ck_merge_queue_ready_at_head`

`verdict <> 'ready' OR ready_sha IS NOT DISTINCT FROM head_sha`. A row claiming
to be ready must be ready *at the commit it is on*, enforced by the database
rather than by every write path remembering to. This is the single guarantee the
table adds over an agent's own memory: an agent remembers "preland said READY"
and does not reliably notice that the thing preland said it about was three
pushes ago. A row cannot forget which commit it was talking about.

**`IS NOT DISTINCT FROM` rather than `=`, and the difference is the whole
constraint.** Written `ready_sha = head_sha`, the row `verdict='ready',
ready_sha=NULL` evaluates FALSE OR NULL, which is NULL — and a CHECK passes on
anything that is not FALSE. So the exact shape the constraint exists to refuse,
a ready verdict with no commit pinned to it, was the one shape it let through.
`IS NOT DISTINCT FROM` is two-valued, and `head_sha` is NOT NULL, so a NULL
`ready_sha` is FALSE and the row is refused.

Chained after 0025 because a single head is what
`test_the_repos_own_migration_chain_is_single_headed` asserts, and
`scripts/migration_reconcile.py` is what to run if a second branch has taken 0026
by the time this lands.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merge_queue_entries",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("base", sa.Text(), nullable=False),
        sa.Column("pr", sa.BigInteger(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("ready_sha", sa.Text(), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("holder", sa.Text(), nullable=False),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("ttl_seconds", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_by", sa.Text(), nullable=True),
        sa.Column("left_reason", sa.Text(), nullable=True),
        sa.Column("lapsed", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.CheckConstraint("pr > 0", name="ck_merge_queue_pr"),
        sa.CheckConstraint("length(btrim(head_sha)) > 0", name="ck_merge_queue_head_sha"),
        sa.CheckConstraint("verdict IN ('ready', 'reconcile', 'queued')",
                           name="ck_merge_queue_verdict"),
        sa.CheckConstraint(
            "verdict <> 'ready' OR ready_sha IS NOT DISTINCT FROM head_sha",
            name="ck_merge_queue_ready_at_head"),
    )
    op.create_index("ix_merge_queue_open", "merge_queue_entries",
                    ["repo", "base", "pr"], unique=True,
                    postgresql_where=sa.text("left_at IS NULL"))
    op.create_index("ix_merge_queue_order", "merge_queue_entries",
                    ["repo", "base", "entered_at", "pr"])


def downgrade() -> None:
    # Wholly reversible, and lossless in the only sense that matters: the table
    # is new, holds nothing derived from anywhere else, and nothing outside it
    # was changed to make room. Rolling back loses the live queue, which is a set
    # of expiring assertions that agents rebuild on their next poll.
    op.drop_index("ix_merge_queue_order", table_name="merge_queue_entries")
    op.drop_index("ix_merge_queue_open", table_name="merge_queue_entries")
    op.drop_table("merge_queue_entries")
