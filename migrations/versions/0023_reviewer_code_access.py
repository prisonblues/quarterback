"""vNEXT: which seats could read the code, and which the host could not carry

`/panel`'s payload has carried `reviewers.<name>.absent` since v2.32 and ingest
dropped it — because `ReviewerIn` declares `populate_by_name=True` with no
`extra=`, so pydantic's default `extra="ignore"` applied. That is not a new class
of mistake here: it is exactly what v2.26's own note in `app/api/reviews.py`
records about `head_sha`, `unread_files`, `provenance_counts` and per-finding
`provenance` — four fields POSTed, four fields dropped, nothing anywhere
reporting it (#93). #113 was about to add two more to the same hole, so all three
land together.

## Why these three are one migration

They are the same fact at three grains, and each is meaningless without the
others when reading a row later:

* `absent` — the box did not carry this vendor's CLI.
* `code_blind` — the seat ran, but reviewed from the diff alone.
* `argv_capped` — the seat's `truncated` was the kernel's doing, not a budget's.

All three are why `coverage_veto` exempts something from the round's confident
stop, and an exemption is only a defensible trade if the thing exempted is
visible afterwards. Without these columns an unattended host that quietly
reviewed with two of four seats is indistinguishable from a full panel, and a
round that stopped confidently with five declared coverage gaps looks like a
round that had none.

## `code_blind` is the confound, not just a flag

It is the most important of the three for anything that ranks reviewers. A seat
that can open the caller and a seat that cannot are not comparable on findings,
on precision, or on `could_not_assess` — `/review/stats` would be averaging two
different jobs. #113's own rule was "either every seat gets it, or the payload
records which did"; this is the column that makes the second half true of the
DATABASE rather than only of a JSON file on somebody's disk.

## Why `argv_capped` is separate from `truncated`

They have opposite remedies. A `max_diff_chars` someone typed can be raised, so
it is evidence about the round. The kernel's per-argument limit cannot, so it is
a property of the box — reported, not counted. Folding the second into the first
would make "truncated" mean two things with different consequences, which is the
distinction the veto turns on.

## The run-level pair

`code_access` is what the round ASKED for; `convention_files_removed` is what had
to be taken out of the reviewers' checkout before any CLI started. The per-seat
answer is `code_blind`, and keeping the setting apart from it is deliberate: a
round with the setting on and every seat blind is a configuration doing nothing,
which is visible in the difference and invisible in either column alone.

`convention_files_removed` is stored because a PR that shipped a `CLAUDE.md` or
an `AGENTS.md` is worth being able to find later. It is the clearest signal
available that a contribution tried to instruct the reviewer judging it, and a
silent strip makes that PR indistinguishable from one that shipped nothing.
`[]` means a tree was built and carried none; NULL means no tree was built at all
(access off, or the fetch failed) — the same NULL/`[]` split `could_not_assess`
already keeps, and for the same reason.

## Nullable, with no backfill

Every column is nullable and none gets a server default. NULL means "the panel
did not say", which is the honest value for every round recorded before this
release — and inventing `false` for those would assert that seats which may well
have been absent or blind were present and sighted. That is the one reading the
data cannot recover from, because it is indistinguishable from a real `false`.

The seats set (`code_access.seats` in the payload) is accepted by the API and
deliberately not stored: it is exactly the reviewers whose `code_blind` is False,
so a column would be a second copy of a fact already on those rows, free to
disagree with them — and the reviewer rows are what a stats query joins.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_reviewers", sa.Column("absent", sa.Boolean(), nullable=True))
    op.add_column("review_reviewers", sa.Column("code_blind", sa.Boolean(), nullable=True))
    op.add_column("review_reviewers", sa.Column("argv_capped", sa.Boolean(), nullable=True))
    op.add_column("review_runs", sa.Column("code_access", sa.Boolean(), nullable=True))
    op.add_column(
        "review_runs",
        sa.Column("convention_files_removed", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
    )


def downgrade() -> None:
    op.drop_column("review_runs", "convention_files_removed")
    op.drop_column("review_runs", "code_access")
    op.drop_column("review_reviewers", "argv_capped")
    op.drop_column("review_reviewers", "code_blind")
    op.drop_column("review_reviewers", "absent")
