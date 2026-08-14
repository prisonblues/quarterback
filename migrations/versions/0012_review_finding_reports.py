"""v2.11: per-reviewer accounts, finding identity, calibration counters

A ``review_findings`` row recorded one title, one detail and a list of reviewer
*names*, because the panel merged before the judge and kept a single member's
text. That can say "codex and pi both reported this" but not what either of them
said — the exact question the stats exist to answer.

``review_finding_reports`` gives each reporter its own row (verbatim account,
its own severity and line), ``review_findings.finding_key`` links observations
of one defect across runs without collapsing them, and three counters on
``review_reviewers`` carry consensus and severity calibration.

Existing findings are backfilled with the same key recipe the app uses
(md5 of file + normalised title), so runs recorded before this migration join
into the same chains.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Must stay identical to ``app.api.reviews._derive_key``: a backfill that keys
#: old rows differently from new ones links nothing across the migration.
_KEY_SQL = (
    "substr(md5(coalesce(file, '') || '|' || "
    "btrim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g'))), 1, 16)"
)


def upgrade() -> None:
    op.create_table(
        "review_finding_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("finding_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("account", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["finding_id"], ["review_findings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("finding_id", "reviewer", name="uq_review_report_finding_reviewer"),
    )
    op.create_index(
        "ix_review_finding_reports_finding", "review_finding_reports", ["finding_id"]
    )
    op.create_index(
        "ix_review_finding_reports_reviewer", "review_finding_reports", ["reviewer"]
    )

    op.add_column("review_findings", sa.Column("finding_key", sa.Text(), nullable=True))
    op.add_column(
        "review_findings",
        sa.Column("related", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(f"UPDATE review_findings SET finding_key = {_KEY_SQL}")
    op.alter_column("review_findings", "finding_key", nullable=False)
    op.create_index("ix_review_findings_key", "review_findings", ["finding_key"])

    for col in ("shared", "sev_stricter", "sev_agree", "sev_looser"):
        op.add_column(
            "review_reviewers",
            sa.Column(col, sa.Integer(), server_default="0", nullable=False),
        )


def downgrade() -> None:
    for col in ("sev_looser", "sev_agree", "sev_stricter", "shared"):
        op.drop_column("review_reviewers", col)
    op.drop_index("ix_review_findings_key", table_name="review_findings")
    op.drop_column("review_findings", "related")
    op.drop_column("review_findings", "finding_key")
    op.drop_table("review_finding_reports")
