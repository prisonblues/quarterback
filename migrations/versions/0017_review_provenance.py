"""v2.26: provenance reaches the board — did the last fix cause this, or miss it?

v2.24 taught the panel to answer that question and gave it nowhere to go. Its
`--json` payload gained four fields — `head_sha`, `unread_files`,
`provenance_counts` and a per-finding `provenance` — and `ReviewIn` is declared
`ConfigDict(populate_by_name=True)` with no `extra=`, so pydantic v2's default
`extra="ignore"` applied: `qb record-review` POSTed all four, ingest discarded
all four, and nothing anywhere said so (#93). The measurement's stated
destination was the board's `/panel` leaderboard, which is precisely the half
that was not built.

The four land as:

* `review_findings.provenance` — **the one that loses something irreplaceable.**
  It is per finding, so it cannot be reconstructed later from anything the board
  keeps. Text rather than an enum type: the vocabulary is the panel's
  (`introduced` / `missed` / `missed-unread` / `unknown`) and it will grow when
  #41 makes attribution exact, at which point a Postgres enum would need its own
  migration to say a word the sender already says.
* `review_runs.head_sha` — nothing else in a run identifies a COMMIT at all;
  `base_branch` holds a branch name. #98 wants the other end of that range and
  #80 wants the column to reason about what a merge actually moved.
* `review_runs.unread_files` — what that round could not read in full, which is
  the next round's `missed-unread` bucket.
* `review_runs.provenance_counts` — the panel's own tally, stored rather than
  derived. See below.

**JSONB for the two lists, not child tables.** `review_run_files` is a table
because it carries per-path attributes and a by-path index for the collision
query it exists to serve. `unread_files` carries neither and nothing reads it by
path; every other list-of-strings on these tables (`stop_veto`,
`could_not_assess`, `reviewers_selected`, `skipped`) is JSONB, and a by-path
question would be a migration on the day it is actually asked.

**`provenance_counts` is stored, not derived from the per-finding column.** The
panel tallies it over the findings the cycle must clear (`outstanding`), while
the rows here also include `dismissed` — so a derivation would silently disagree
with the panel's own statement of its own round. It also carries a distinction
no derivation can: `{}` means the question does not arise (round 1, or outside a
cycle), which is not all-zero ("could have attributed, and had nothing to").

**The scorecard counters are the axis #48 was filed for.** #48's stated payoff is
"which reviewers find *pre-existing* defects versus which mostly catch
regressions in fresh code", which is per reviewer — so the four buckets are
tallied onto `review_reviewers` at ingest, exactly as `p1`..`p4` and `solo`
already are, and `/review/stats` sums them instead of joining three tables to
re-derive per-reviewer attribution. Derived server-side from the findings, so a
scorecard cannot contradict what it summarises.

**Every new column is nullable, and null means NOT RECORDED.** Never "no
provenance". A pre-v2.26 run has none because nothing stored it; a finding
outside a cycle has none *because the question does not arise*; `unknown` is a
real bucket for a finding that was asked about and could not be placed. #89's
panel side already keeps those three apart and the board must not collapse them
on the way in. The counters are the one exception — NOT NULL, defaulting to 0,
like every sibling counter on that table — so `/review/stats` publishes a
`provenance_runs` coverage marker beside them, the same way `token_runs` marks
how much of a token sum is real.

The revision number and the release number are unrelated counters: this is schema
revision **0017** and it ships in product version **v2.26**.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The four counters, and the bucket each one tallies. Spelled here rather than
#: inline so `upgrade` and `downgrade` cannot drift apart.
COUNTERS = ("prov_introduced", "prov_missed", "prov_missed_unread", "prov_unknown")


def upgrade() -> None:
    # The commit the round reviewed. Text, not a fixed-width char: a sha arrives
    # as whatever `gh` printed, and the API normalises rather than the column
    # truncating.
    op.add_column("review_runs", sa.Column("head_sha", sa.Text(), nullable=True))
    op.add_column("review_runs",
                  sa.Column("unread_files", postgresql.JSONB(), nullable=True))
    op.add_column("review_runs",
                  sa.Column("provenance_counts", postgresql.JSONB(), nullable=True))

    # The per-finding bucket — the irreplaceable one. No index: every read path
    # today reaches findings BY RUN (`GET /review/{id}`, `GET /review/findings`)
    # and the window aggregate in `/review/stats` groups a set already narrowed
    # by the run join, so an index on this column would be write cost on the
    # largest table this feature touches for a scan nothing performs. Same
    # reasoning `review_run_files` records for deliberately not indexing `run_id`.
    op.add_column("review_findings", sa.Column("provenance", sa.Text(), nullable=True))

    # Per-reviewer tallies. NOT NULL with a server default, like every sibling
    # counter: a scorecard is written in full by ingest, and a nullable counter
    # would put "this member raised no introduced defects" and "this run predates
    # provenance" in one column with no way to tell them apart. The second fact
    # lives on the run (`provenance_counts IS NOT NULL`), which is where
    # `/review/stats` reads it from for the coverage marker.
    for col in COUNTERS:
        op.add_column("review_reviewers",
                      sa.Column(col, sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    for col in reversed(COUNTERS):
        op.drop_column("review_reviewers", col)
    op.drop_column("review_findings", "provenance")
    op.drop_column("review_runs", "provenance_counts")
    op.drop_column("review_runs", "unread_files")
    op.drop_column("review_runs", "head_sha")
