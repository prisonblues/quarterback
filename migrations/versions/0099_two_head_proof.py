"""throwaway: a deliberate second head, to prove the migration-heads CI job goes red.

Not for merging. It exists to show the `migration-heads` job refusing a real two-headed
graph on a real pull request, and the branch is deleted immediately afterwards.
"""

from collections.abc import Sequence

revision: str = "0099"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
