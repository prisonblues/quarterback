"""v2.23: the PR's changed-file list — the datum collision ordering needs

The board could say a merge changed 2,032 lines and not which files. The only
paths it held were the ones findings happened to name — nine, for a run whose PR
touched more — which is a proxy for the diff and not the diff. So "which open PRs
does this merge disturb?" was unanswerable, and #73's disjointness from #62 was
discovered by trying rather than by asking.

One row per (run, path), not a JSONB array on the run: the query this exists for
reads BY path and fans out to runs, which is an index seek against a child table
and a full scan against a JSONB column. It is also stored per RUN rather than per
PR, for the same reason findings are — a PR's file set grows while it is open,
and a row overwritten in place cannot say what Tuesday's round was looking at.

`changed_files_total` is GitHub's own count and is deliberately NOT derivable
from the rows: `gh` pages the files connection and GitHub caps a PR's file list
at 3,000, so the two are allowed to disagree, and their disagreement is the only
signal that a collision query over this run under-reports. Storing only the rows
would present a truncated list as a complete one.

Nullable, like every other column added to `review_runs` after the fact: a run
recorded before the panel sent this has NO file list, which is not the same fact
as a PR that changed no files, and a consumer must be able to tell them apart.

Paths, not hunk ranges (#82 decided this explicitly): paths answer "will these
two PRs collide", ranges answer "and exactly where", and nothing asks the second
yet. `additions`/`deletions` are here only because the same `gh pr view` call
already returns them, and they turn `changed_lines` from a bare total into
something attributable to a file.

The revision number and the release number are unrelated counters: this is schema
revision **0016** and it ships in product version **v2.23**.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_runs", sa.Column("changed_files_total", sa.Integer(), nullable=True))
    # The board held no PR state at all, so a collision query could not tell a
    # live rival from one merged last week and reported both. Recorded per run
    # because that is the only moment the board talks to GitHub: it is the state
    # as of that panel, which is the same currency as the file list beside it.
    op.add_column("review_runs", sa.Column("pr_state", sa.Text(), nullable=True))
    op.add_column("review_runs", sa.Column("is_draft", sa.Boolean(), nullable=True))
    op.create_table(
        "review_run_files",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=True),
        sa.Column("deletions", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # A payload repeating a path is a sender bug; letting it through would
        # double that file's weight in every collision count built on this table.
        # Its B-tree on (run_id, path) is also what serves every run_id-only
        # lookup, via the leftmost prefix — so no separate index on run_id: that
        # would be pure storage and write overhead on the largest table this
        # feature creates.
        sa.UniqueConstraint("run_id", "path", name="uq_review_run_file_run_path"),
    )
    # The collision index, and (path, run_id) rather than (path) alone: the query
    # it exists for reads WHERE path IN (…) and wants run_id back, which this
    # answers from the index without a heap fetch per matching row. Nothing
    # queries by bare path, so the composite strictly dominates.
    op.create_index("ix_review_run_files_path", "review_run_files", ["path", "run_id"])


def downgrade() -> None:
    op.drop_index("ix_review_run_files_path", table_name="review_run_files")
    op.drop_table("review_run_files")
    op.drop_column("review_runs", "is_draft")
    op.drop_column("review_runs", "pr_state")
    op.drop_column("review_runs", "changed_files_total")
