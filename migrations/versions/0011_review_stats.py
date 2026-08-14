"""v2.10: reviewer-panel stats — runs, per-reviewer scorecards, findings

The panel already reviews one diff with several models and has a judge rule each
finding real or not; it just threw the comparison away when the process exited.
These three tables keep it, so "which model finds the most real issues" and "is
the expensive tier worth its cost" become queries over accumulated runs.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("pr", sa.Integer(), nullable=False),
        sa.Column("pr_title", sa.Text(), nullable=True),
        sa.Column("base_branch", sa.Text(), nullable=True),
        sa.Column("changed_lines", sa.Integer(), nullable=True),
        sa.Column("diff_chars", sa.Integer(), nullable=True),
        sa.Column("diff_truncated", sa.Boolean(), nullable=True),
        sa.Column("judged", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("judge_model", sa.Text(), nullable=True),
        sa.Column("judge_skip", sa.Text(), nullable=True),
        sa.Column("sonar_gate", sa.Text(), nullable=True),
        sa.Column("ci_status", sa.Text(), nullable=True),
        sa.Column("reviewers_selected", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reviewers_override", sa.Text(), nullable=True),
        sa.Column("skipped", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("n_confirmed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_dismissed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_unjudged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_sonar", sa.Integer(), server_default="0", nullable=False),
        sa.Column("run_key", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key"),
    )
    op.create_index("ix_review_runs_repo_pr", "review_runs", ["repo", "pr"])
    op.create_index("ix_review_runs_ts", "review_runs", ["ts"])
    op.create_index("ix_review_runs_author", "review_runs", ["author"])

    op.create_table(
        "review_reviewers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("effort", sa.Text(), nullable=True),
        sa.Column("ran", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("max_diff_chars", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("raised", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confirmed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dismissed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unjudged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("solo", sa.Integer(), server_default="0", nullable=False),
        sa.Column("p1", sa.Integer(), server_default="0", nullable=False),
        sa.Column("p2", sa.Integer(), server_default="0", nullable=False),
        sa.Column("p3", sa.Integer(), server_default="0", nullable=False),
        sa.Column("p4", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "name", name="uq_review_reviewer_run_name"),
    )
    op.create_index("ix_review_reviewers_name_model", "review_reviewers", ["name", "model"])

    op.create_table(
        "review_findings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("file", sa.Text(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reviewers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("n_reviewers", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_findings_run", "review_findings", ["run_id"])
    op.create_index("ix_review_findings_verdict", "review_findings", ["verdict"])


def downgrade() -> None:
    op.drop_table("review_findings")
    op.drop_table("review_reviewers")
    op.drop_table("review_runs")
