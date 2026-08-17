"""v2.39: the plan — an ordered, claimable list of what is next

The board could already answer *who is here*, *what are they touching*, *what
did they just publish* and *what did the panel find*. It could not answer **what
is next, in what order, and who has it** — the question every agent asks first
and every agent answered by guessing.

That knowledge lived in three places, none of them the board: 26 unordered
GitHub issues, a human repeating the sequence to each agent that asked, and an
untracked `plan.md` on `zeus` — invisible from `hermes`, invisible from a
container, and gone with the checkout. `epic.py`'s per-run state
(`~/.local/state/loops/epic-*.json`) and the panel's round baselines
(`/tmp/panel-<pr>-r<n>.json`) are the same shape and the same flaw: real item
state, real resume semantics, visible only to the process that wrote it.

## What this table is NOT

**Not a copy of the issues.** An item carries a `ref` and a one-line title, and
nothing else about the work: the issue holds the *what* and the *why*, and the
plan holds only what an issue cannot — the order, the reasoning behind the
order, the dependencies, and who has it right now. The partial unique index
below enforces that at the database: one open item per referenced issue, so the
plan cannot come to hold two contradictory rows about #60.

**Not a project-management tool.** No estimates, no sprints, no burndown, no
assignee field. Ordered items, refs, dependencies, and a claim.

**Not #53's review queue.** Both are "work items with state", and they are not
the same object: a review job is machine-generated and self-clearing, a plan
item is human intent that outlives many sessions. Separate table, and the claim
mechanism is shared rather than reimplemented — see below.

## No second implementation of "who has this"

There is deliberately **no holder column here**. An item is taken when a live
row exists in `resource_leases` (v2.31) for it, which buys three things for
free: atomicity from the partial unique index, passive TTL expiry with no
reaper — so a dead agent's claim disappears without anyone intervening, which is
one of this issue's acceptance criteria — and, because the key for an
issue-backed item is exactly the `work` key agents are *already* taking by hand
(`kind='work'`, `key='prisonblues/quarterback#142'`), a claim taken through the
plain `POST /claim` shows up in the plan without the claimant doing anything.
Two implementations of "who has this right now" is the outcome #99 was filed to
avoid, and this is the third feature to want one.

## Ordering

`rank` is a plain integer rewritten wholesale by `POST /plan/reorder`, which is
a human-only endpoint. Fractional ranks would let any agent slot itself in
without a rewrite — which is precisely the thrash the issue asks to prevent, so
the cheap-insert property is not wanted here. Ties fall back to `created_at`
and `id`, so ordering is total even mid-rewrite.

`state` is `open | done | dropped`. The plan never *decides* an item is done:
`done` records that the linked issue closed, and git ancestry / GitHub remain
the authority, exactly as `epic.py` already had it right — *"the file is the
fast path + audit trail"*.

The revision number and the release number are unrelated counters: this is
schema revision **0021** and it ships in the release stamped at land.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # NULL means fleet-wide: the plan spans repos, as does the fleet, and an
        # item like "rebuild home-manager on every box" belongs to no repo.
        sa.Column("repo", sa.Text(), nullable=True),
        # One line. Deliberately not a description column — the issue holds that,
        # and a second place to write it is a second place for it to be wrong.
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("ref_kind", sa.Text(), nullable=True),
        sa.Column("ref_value", sa.Text(), nullable=True),
        # "stage 1" / "after the panel work". Free text: phases are the human's
        # vocabulary and change more often than a migration should.
        sa.Column("phase", sa.Text(), nullable=True),
        sa.Column("rank", sa.BigInteger(), nullable=False),
        # Item ids this one waits on. JSONB rather than a join table: it is read
        # whole on every plan read, never queried across, and a five-row edge
        # table for a list of uuids is ceremony. Cycles are refused at the API.
        sa.Column("depends_on", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"),
                  nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        # WHY this sits here — the half that has no home in an issue. This is the
        # sentence a human would otherwise repeat to each agent that asks.
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # Last touched, so a phase that has not moved in a fortnight is visibly
        # stale rather than quietly wrong. A plan nobody updates is worse than no
        # plan, because it is believed.
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by", sa.Text(), nullable=True),
        sa.CheckConstraint("state IN ('open','done','dropped')", name="ck_plan_items_state"),
        # A ref is both halves or neither: a ref_kind with no value is a link to
        # nowhere, and a value with no kind cannot be rendered or claimed.
        sa.CheckConstraint(
            "(ref_kind IS NULL) = (ref_value IS NULL)", name="ck_plan_items_ref_pair"
        ),
    )
    # The read path: every plan read is "this repo's items, in order".
    op.create_index("ix_plan_items_repo_rank", "plan_items", ["repo", "rank"])
    # "One open item per issue" — the promise that the plan cannot contradict an
    # issue, kept by the database rather than by everyone remembering. COALESCE
    # because NULL repos (fleet items) are distinct from each other under a plain
    # unique index, which would let the same fleet ref be added twice.
    op.create_index(
        "ix_plan_items_open_ref", "plan_items",
        [sa.text("COALESCE(repo, '')"), "ref_kind", "ref_value"], unique=True,
        postgresql_where=sa.text("ref_value IS NOT NULL AND state = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ix_plan_items_open_ref", table_name="plan_items")
    op.drop_index("ix_plan_items_repo_rank", table_name="plan_items")
    op.drop_table("plan_items")
