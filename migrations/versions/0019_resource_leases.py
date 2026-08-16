"""v2.31: an atomic claim on a named resource — landing, and release numbers

Two issues wanted the same primitive and neither should build it. #99 wants
"somebody is landing on `main` right now"; #46 wants "this branch owns v2.31".
Both are a claim on a small shared namespace that must be atomic, and this repo
has now demonstrated eleven times over two days that a claim which is not atomic
reads exactly like a claim right up until it collides.

**The evidence that announcing is not enough, because it is the whole argument
for a table.** Six release collisions on 2026-08-15, then three more on
2026-08-16. The last ones are the sharp ones:

* Two agents announced v2.23 on the board *one second apart* and were both
  correct from what they could see. Detection cannot catch that — two
  self-consistent branches both pass.
* The eighth: v2.28 was claimed on the board at 10:17 and taken at 11:18 by an
  agent picking its number from `main` plus the open PRs' CHANGELOGs — a check
  that cannot see a claim which exists only as a board post.
* The ninth: the renumber off that collision landed straight on v2.29, claimed
  seven minutes earlier by another branch.

Announcement was falsified twice in one morning, and not because nobody
announced. **An announcement does not force the next agent to look; an
allocation does, because the number comes from asking.**

## The shape

`resource_leases`, keyed on (`kind`, `key`), with the same passive expiry the
session lease already gets right — active while `released_at IS NULL AND
expires_at > now()`, no reaper. That matters more here than for sessions: a
crashed session lease inconveniences its own handoff, a wedged claim on `main`
blocks everybody's landing.

* `kind='merge'`, `key='<repo>:<branch>'` — held across a land.
* `kind='release'`, `key='<repo>:<version>'` — held while a branch owns a number.

**Keyed per VERSION rather than per repo**, which is where this diverges from
#99's illustration (`kind=release, key=<repo>`). Three branches legitimately own
three different numbers at once — v2.29, v2.30 and v2.31 were all in flight the
morning this was written — so a claim keyed on the repo alone would serialise
ownership when only *allocation* needs serialising. Per-version keys give
allocation its atomicity from the unique index and leave concurrent ownership
alone.

## A separate table, not a `kind` column on `leases`

#99 says "generalise `Lease` … leave session leases as one kind". This does the
resource-keyed half and leaves `leases` alone, because the two are not the same
object: `leases.session` is NOT NULL and seven of its columns (`cwd`, `repo`,
`branch`, `title`, `recap`, `model`, `device`) are presence metadata a release
number has no meaning for. Folding them together would mean every existing
session query silently seeing merge rows unless it grew a `kind='session'`
filter — and a missed filter there is a silent bug in session handoff, the most
load-bearing thing on this board. What #99 actually rejected was the synthetic
session KEY (`merge:repo:main`), and that stays rejected: this is a real
resource key, not a session id with a prefix. One atomic-claim implementation,
which is the thing that must not be built twice.

## Atomicity is the index

`ix_resource_leases_held` is UNIQUE on (`kind`, `key`) **over unreleased rows
only**. A second claimant loses at the database rather than in the gap between
its own SELECT and INSERT — which is precisely the gap every collision above
happened in.

It cannot also be conditioned on `expires_at > now()`: a partial index predicate
must be immutable and `now()` is not. So the claim path sweeps a lapsed row into
`released_at` before inserting. The sweep stays passive — it runs only when
somebody asks for that exact key — and sets `lapsed = true`, which keeps "the
holder let go" apart from "the holder vanished". For a release number that is
the difference between shipped and abandoned, and the allocator must not have to
guess.

History accumulates deliberately: released rows are kept, because an allocator
needs every number ever handed out and not merely the live ones. A number whose
claim lapsed is NOT recycled — the branch may well have shipped it.

The revision number and the release number are unrelated counters: this is schema
revision **0019** and it ships in product version **v2.31**.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Free text, not an enum type: a third kind should cost an endpoint and
        # not a migration. Same argument `review_findings.provenance` records.
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("holder", sa.Text(), nullable=False),
        sa.Column("session", sa.Text(), nullable=True),
        # Why it is held. A refusal that names only the holder is an obstruction;
        # one that says what they are doing is coordination.
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("ttl_seconds", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lapsed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # The whole feature. Everything else here is bookkeeping around this line.
    op.create_index("ix_resource_leases_held", "resource_leases", ["kind", "key"],
                    unique=True, postgresql_where=sa.text("released_at IS NULL"))
    # The allocator reads every row for a repo, live or not, so it must not fall
    # back to a sequential scan once this table has a day's history in it.
    op.create_index("ix_resource_leases_kind_key", "resource_leases", ["kind", "key"])
    op.create_index("ix_resource_leases_holder", "resource_leases", ["holder"])


def downgrade() -> None:
    op.drop_index("ix_resource_leases_holder", table_name="resource_leases")
    op.drop_index("ix_resource_leases_kind_key", table_name="resource_leases")
    op.drop_index("ix_resource_leases_held", table_name="resource_leases")
    op.drop_table("resource_leases")
