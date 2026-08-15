"""v2.19: per-reviewer token usage — the cost half of the scorecard

v2.10 recorded what each panel member *found* and v2.13's `duration_ms` recorded
how long it took. Neither says what it cost, so the /panel leaderboard could rank
a reviewer top on confirmed findings while it was quietly the most expensive seat
on the panel.

Wall-clock is the cross-vendor axis. These columns are the other half, and they
answer a narrower question: *within* one vendor, is the expensive tier worth it —
opus over sonnet, codex `xhigh` over `medium`. Same tokenizer, same cache
semantics, so directly comparable, which is exactly the grouping
`GET /review/stats` already does by (reviewer, model, effort).

All nullable: the panel reads usage back out of a pinned session after the fact,
and a transcript that could not be read loses a number and nothing else. A null
here means "not recorded", never "zero".

`cost_usd` is stored only where the *vendor states it* (pi does; codex states
none; claude states it on stdout but not in the transcript the retrospective path
reads). It is deliberately never derived from a price table — a run priced at
today's rates is silently wrong when queried in six weeks, and the whole point of
this table is that it stays true later.

The revision number and the release number are unrelated counters: this is schema
revision **0015** and it ships in product version **v2.19**. It chains after 0014
(the v2.15 review rounds), which landed on main while this branch was open.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_reviewers", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("review_reviewers", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "review_reviewers", sa.Column("cached_input_tokens", sa.Integer(), nullable=True)
    )
    op.add_column("review_reviewers", sa.Column("reasoning_tokens", sa.Integer(), nullable=True))
    # Numeric, not float: a cost is money, and the sub-cent digits of a cheap run
    # are exactly what a sum over hundreds of runs is made of.
    op.add_column("review_reviewers", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("review_reviewers", "cost_usd")
    op.drop_column("review_reviewers", "reasoning_tokens")
    op.drop_column("review_reviewers", "cached_input_tokens")
    op.drop_column("review_reviewers", "output_tokens")
    op.drop_column("review_reviewers", "input_tokens")
