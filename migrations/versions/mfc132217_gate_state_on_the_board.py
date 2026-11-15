"""Gate state on the board: head under test, stage, clean-run count (#852)

One row per (repo, branch), so a promotion gate's state is a board object a peer
or a successor session can read, instead of comments on an issue. See
:class:`app.models.gate.Gate`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mfc132217"
down_revision: str | None = "m9c82a7e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gates",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("clean_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exit_bar", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("last_result", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outputs", sa.Text(), nullable=True),
        sa.Column("reset_rule", sa.Text(), nullable=True),
        sa.Column("restarted_from", sa.Text(), nullable=True),
        sa.Column("restarted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("repo = lower(btrim(repo)) AND length(repo) > 0",
                           name="ck_gates_repo_canonical"),
        sa.CheckConstraint("length(btrim(branch)) > 0", name="ck_gates_branch"),
        sa.CheckConstraint("head_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'",
                           name="ck_gates_head_sha"),
        sa.CheckConstraint("clean_runs >= 0", name="ck_gates_clean_runs"),
        sa.CheckConstraint("exit_bar >= 1", name="ck_gates_exit_bar"),
        sa.CheckConstraint("last_result IS NULL OR last_result IN ('clean', 'failed')",
                           name="ck_gates_last_result"),
        sa.CheckConstraint("stage IS NULL OR length(stage) <= 200",
                           name="ck_gates_stage_len"),
    )
    op.create_index("ix_gates_repo_branch", "gates", ["repo", "branch"], unique=True)


def downgrade() -> None:
    # The table is new and nothing outside it changed to make room, so rolling it
    # back returns a gate to living in issue comments, as it did before #852.
    op.drop_index("ix_gates_repo_branch", table_name="gates")
    op.drop_table("gates")
